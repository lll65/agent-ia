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
from datetime import datetime

from agent.memory import save_message, get_history, clear_history
from memory.chroma_store import ChromaStore
from config import config

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self):
        self.backend = None     # SupabaseStore persistant (prioritaire)
        self.chroma = None      # ChromaDB local (fallback sémantique)
        self._summary_cache: dict[str, str] = {}

        # 1. Tente le backend persistant Supabase
        if config.SUPABASE_DB_URL:
            try:
                from memory.supabase_store import SupabaseStore
                store = SupabaseStore(config.SUPABASE_DB_URL)
                if store.available:
                    self.backend = store
                    logger.info("Mémoire: backend Supabase (persistant, pgvector).")
            except Exception as e:
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

        # Historique récent
        recent = self.recall_recent(agent_id, recent_limit)
        if recent:
            history_text = "\n".join(
                f"{m['role'].upper()}: {m['content'][:400]}"
                for m in recent
            )
            parts.append(f"[HISTORIQUE RÉCENT]\n{history_text}")

        return "\n\n".join(parts)

    # ── Résumé ───────────────────────────────────────────────────────────────

    def cache_summary(self, agent_id: str, summary: str):
        self._summary_cache[agent_id] = summary

    def get_summary(self, agent_id: str) -> str | None:
        return self._summary_cache.get(agent_id)

    def should_summarize(self, agent_id: str) -> bool:
        history = self.recall_recent(agent_id, limit=config.SUMMARY_THRESHOLD + 1)
        return len(history) >= config.SUMMARY_THRESHOLD

    # ── Effacement ───────────────────────────────────────────────────────────

    def clear(self, agent_id: str):
        if self.backend:
            self.backend.clear(agent_id)
        else:
            clear_history(agent_id)
            if self.chroma and self.chroma.available:
                self.chroma.delete_collection(agent_id)
        self._summary_cache.pop(agent_id, None)
