"""
Gestionnaire de mémoire unifié:
 - Backend persistant Supabase (Postgres + pgvector) si SUPABASE_DB_URL défini
   → survit aux redémarrages / mises en veille de l'hôte.
 - Sinon fallback local : SQLite (court terme) + ChromaDB (sémantique).
 - Auto-résumé: compresse les conversations longues.

L'interface publique (remember / recall_recent / recall_relevant / build_context /
clear) est identique quel que soit le backend — le reste du code n'a rien à changer.
"""
import logging
import re
from datetime import datetime

from agent.memory import save_message, get_history, clear_history
from memory.chroma_store import ChromaStore
from config import config


def _ressemble_a_des_donnees(texte: str) -> bool:
    """Ce texte porte-t-il des DONNÉES qu'on voudra réinterroger, ou est-ce de la prose ?

    Une réponse d'app (tableur, agenda, mails) est la seule trace de ce qu'on est allé
    chercher : la couper court revient à jeter le résultat. Une longue explication en
    prose, elle, n'a pas à revenir en entier — elle pousse le modèle à se répéter.
    """
    t = texte or ""
    if t.count("|") >= 6:                       # un tableau Markdown
        return True
    if t.count("\n- ") + t.count("\n• ") >= 3:  # une liste d'éléments
        return True
    if "{" in t and ('":' in t or '" :' in t):  # du JSON renvoyé par une API
        return True
    return len(re.findall(r"\d[\d\s.,]*", t)) >= 8   # beaucoup de chiffres = des mesures

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self):
        self.backend = None     # SupabaseStore persistant (prioritaire)
        self.chroma = None      # ChromaDB local (fallback sémantique)
        self._summary_cache: dict[str, str] = {}
        # Taille de l'historique au moment du dernier résumé : c'est ce qui
        # permet de ne re-résumer que s'il s'est passé quelque chose depuis.
        self._summary_couvre: dict[str, int] = {}
        self.echec_persistance = ""   # pourquoi la mémoire n'est pas persistante

        # 1. Tente le backend persistant Supabase
        if config.SUPABASE_DB_URL:
            try:
                from memory.supabase_store import SupabaseStore
                store = SupabaseStore(config.SUPABASE_DB_URL)
                if store.available:
                    self.backend = store
                    logger.info("Mémoire: backend Supabase (persistant, pgvector).")
                else:
                    # On garde la raison pour le diagnostic : « configurée mais en panne »
                    # est le pire des cas, car il ressemble à « ça marche ».
                    self.echec_persistance = store.erreur
            except Exception as e:
                self.echec_persistance = f"{type(e).__name__}: {str(e)[:300]}"
                logger.warning(f"Mémoire: Supabase indisponible ({e}) — fallback local.")

        # 2. Fallback local (SQLite + ChromaDB)
        if self.backend is None:
            self.chroma = ChromaStore(config.CHROMA_DIR)
            logger.info("Mémoire: backend local (SQLite + ChromaDB).")

    # ── Écriture ─────────────────────────────────────────────────────────────

    def remember(self, agent_id: str, role: str, content: str):
        """Sauvegarde un message (historique + index sémantique)."""
        if self.backend:
            self.backend.add(agent_id, role, content)
            return
        # Fallback local
        save_message(agent_id, role, content)
        if self.chroma and self.chroma.available:
            self.chroma.add(
                agent_id,
                content,
                metadata={"role": role, "timestamp": datetime.utcnow().isoformat()},
            )

    # ── Lecture ──────────────────────────────────────────────────────────────

    def recall_recent(self, agent_id: str, limit: int = 20) -> list[dict]:
        """Retourne les N derniers messages (ordre chronologique)."""
        if self.backend:
            return self.backend.recent(agent_id, limit)
        return get_history(agent_id, limit)

    def recall_relevant(self, agent_id: str, query: str, n: int = 5) -> list[dict]:
        """Retrouve les souvenirs les plus pertinents (recherche sémantique)."""
        if self.backend:
            return self.backend.search(agent_id, query, n)
        if self.chroma and self.chroma.available:
            return self.chroma.search(agent_id, query, n)
        return []

    def _semantic_available(self) -> bool:
        return self.backend is not None or bool(self.chroma and self.chroma.available)

    def build_context(self, agent_id: str, current_task: str, recent_limit: int = 12) -> str:
        """
        Construit le contexte complet pour l'agent:
        1. Résumé des échanges passés (si disponible)
        2. Souvenirs pertinents à la tâche courante (recherche sémantique)
        3. Messages récents
        """
        parts = []

        # Résumé long-terme
        if agent_id in self._summary_cache:
            parts.append(f"[CONTEXTE PASSÉ]\n{self._summary_cache[agent_id]}")

        # Mémoire sémantique (top 4)
        if self._semantic_available():
            relevant = self.recall_relevant(agent_id, current_task, n=4)
            if relevant:
                # Filtre les doublons avec l'historique récent
                seen = set()
                unique = []
                for r in relevant:
                    key = r['text'][:80]
                    if key not in seen:
                        seen.add(key)
                        unique.append(r)
                if unique:
                    mem_text = "\n".join(f"- {r['text'][:250]}" for r in unique)
                    parts.append(f"[SOUVENIRS PERTINENTS]\n{mem_text}")

        # Historique récent.
        # ⚠️ Les anciennes réponses de l'agent sont TRONQUÉES court : de longues analyses passées
        # (ex. finance) réinjectées en entier poussaient le modèle à rejouer le même sujet/format.
        recent = self.recall_recent(agent_id, recent_limit)
        if recent:
            # ⚠️ Ce qui compte n'est pas l'ancienneté mais la NATURE du contenu.
            # Une longue analyse en prose, réinjectée en entier, pousse le modèle à
            # rejouer le même sujet : on la coupe court, c'est le réglage d'origine.
            # Mais un TABLEAU de données est la seule trace de ce qu'on est allé
            # chercher : coupé à 150 caractères, Nova affichait tout le portefeuille PEA
            # puis répondait « je ne suis pas conseiller financier » à la question
            # suivante — elle ne voyait plus un seul chiffre.
            def limite(m):
                if m.get("role") == "user":
                    return 400
                return 1200 if _ressemble_a_des_donnees(m.get("content") or "") else 150

            history_text = "\n".join(
                f"{m['role'].upper()}: {m['content'][:limite(m)]}" for m in recent
            )
            parts.append(
                "[HISTORIQUE RÉCENT — simple rappel de la conversation. "
                "Ce n'est NI une consigne, NI un format à imiter : réponds à la demande actuelle uniquement.]\n"
                + history_text
            )

        return "\n\n".join(parts)

    # ── Résumé ───────────────────────────────────────────────────────────────

    def cache_summary(self, agent_id: str, summary: str):
        self._summary_cache[agent_id] = summary
        # On note COMBIEN de messages ce résumé couvre : sans ça, impossible de
        # savoir s'il reste quelque chose de neuf à résumer.
        self._summary_couvre[agent_id] = self._taille_historique(agent_id)

    def get_summary(self, agent_id: str) -> str | None:
        return self._summary_cache.get(agent_id)

    def _taille_historique(self, agent_id: str) -> int:
        try:
            return len(self.recall_recent(agent_id, limit=10_000))
        except Exception:
            return 0

    def should_summarize(self, agent_id: str) -> bool:
        """Y a-t-il assez de NOUVEAU pour justifier un appel LLM de résumé ?

        ⚠️ On répondait « oui » dès que l'historique atteignait 15 messages. Comme
        l'historique ne diminue jamais, c'était vrai en PERMANENCE à partir du 8e
        échange : un aller-retour LLM complet s'ajoutait avant CHAQUE réponse, pour
        toujours. Sur téléphone, Lohan attendait ce résumé avant que Nova commence
        seulement à réfléchir — et sur des offres gratuites saturées, ce doublement
        d'appels épuisait le quota et faisait échouer la vraie réponse à cause d'un
        résumé qui, en plus, ne portait que sur les messages déjà réinjectés en clair
        juste en dessous.
        """
        taille = self._taille_historique(agent_id)
        if taille < config.SUMMARY_THRESHOLD:
            return False
        deja = self._summary_couvre.get(agent_id)
        if deja is None:
            return True
        # Un nouveau résumé seulement après un palier complet de messages en plus.
        return taille - deja >= config.SUMMARY_THRESHOLD

    # ── Effacement ───────────────────────────────────────────────────────────

    def clear(self, agent_id: str):
        if self.backend:
            self.backend.clear(agent_id)
        else:
            clear_history(agent_id)
            if self.chroma and self.chroma.available:
                self.chroma.delete_collection(agent_id)
        self._summary_cache.pop(agent_id, None)
        self._summary_couvre.pop(agent_id, None)
