"""
Moteur ReAct — Reasoning + Acting loop avec mémoire et plugins.
"""
import json
import re
import logging
import asyncio
import time as _tm
from config import config

logger = logging.getLogger(__name__)

# Directive compacte (le MASTER complet ~2500 tokens dépassait la limite Groq 12k tok/min)
try:
    from agent.system_prompt import AGENT_COMPACT_DIRECTIVE as _MASTER_SYS
except ImportError:
    _MASTER_SYS = ""

SYSTEM_TEMPLATE = """{master_directives}

---

## AGENT ACTIF : {name}
{description}

## OUTILS DISPONIBLES :
{tools_list}

## PROTOCOLE D'ACTION STRICT

Pour utiliser un outil, réponds EXACTEMENT dans ce format :
THOUGHT: analyse en une phrase — pourquoi cet outil, quelles données tu attends
ACTION: nom_exact_de_l_outil
PARAMS: {{"param": "valeur"}}

⚠️ Écris le nom de l'outil NU, sans crochets, sans guillemets, sans backticks, sans gras.
Exemple correct :   ACTION: search_web
Exemples à éviter : ACTION: [search_web] · ACTION: `search_web` · **ACTION:** search_web

Pour donner la réponse finale (quand tu as toutes les informations nécessaires) :
THOUGHT: synthèse — que vas-tu livrer
FINAL: réponse complète, structurée, actionnelle

## RÈGLES D'EXÉCUTION
1. N'invente JAMAIS une observation — attends toujours l'OBSERVATION réelle de l'outil.
2. QUESTION FACTUELLE (actualité, tendances, marché, prix, événements récents, "en 2026", chiffres réels,
   idées/analyses qui dépendent du contexte actuel) : ta PREMIÈRE action DOIT être `search_web`.
   N'exécute PAS de code Python pour "inventer" des données qui devraient venir du web.
3. ANTI-HALLUCINATION : ne cite JAMAIS une source, une date ou un chiffre précis sans qu'un OUTIL te l'ait
   réellement renvoyé. Sans appel d'outil correspondant → écris "estimation non vérifiée".
4. SUJET RESPECTÉ : ne parle de bourse, actions, ETF, crypto, marchés, investissement ou épargne QUE si
   l'utilisateur le demande explicitement. N'utilise le format financier (entrée/TP/stop-loss) que pour
   l'analyse d'un actif précis réellement demandée. Ne change jamais de sujet de toi-même.
5. LONGUEUR PROPORTIONNELLE : remarque simple ou question courte → réponse courte (1-3 phrases), sans titres
   ni plan d'action. Réserve les réponses structurées aux vraies demandes complexes.
6. FINAL directement exploitable. Jamais "je ne peux pas" sans alternative.
7. CHIFFRES DE MARCHÉ : un cours, un indice (CAC 40, S&P 500), un prix de crypto ou une statistique ne
   peuvent JAMAIS sortir de ta mémoire. Sans OBSERVATION d'outil correspondante, tu n'en cites aucun.
8. DONNÉES PERSONNELLES (agenda, événements, mails, contacts, fichiers, messages) : elles ne peuvent venir
   QUE d'un outil (connected_app). Si aucun OUTIL ne te les a réellement renvoyées, ou si l'outil a échoué,
   tu DOIS le dire clairement ("je n'ai pas pu accéder à…"). Inventer un agenda, un mail ou un rendez-vous
   est une faute GRAVE et strictement interdite — même si le résultat semble plausible.
"""


_JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
         "août", "septembre", "octobre", "novembre", "décembre")


def _JOURS_COURT(d) -> str:
    """« 18 août 2026 » — date en clair pour cibler l'actualité du jour."""
    return f"{d.day} {_MOIS[d.month - 1]} {d.year}"


def date_du_jour() -> str:
    """Date courante en clair. Sans elle, le modèle raisonne avec l'année de son
    entraînement (d'où des recherches en « 2024 ») et se trompe sur « aujourd'hui »."""
    from datetime import datetime
    d = datetime.now()
    return (f"Nous sommes le {_JOURS[d.weekday()]} {d.day} {_MOIS[d.month - 1]} {d.year}, "
            f"il est {d.strftime('%H:%M')}. L'année en cours est {d.year}.")


def build_system(agent_config: dict, plugins: dict) -> str:
    available  = agent_config.get("tools") or list(plugins.keys())
    tools_list = "\n".join(
        f"  • {name}: {desc}"
        for name, desc in plugins.items()
        if name in available
    )
    return f"[DATE] {date_du_jour()}\n\n" + SYSTEM_TEMPLATE.format(
        master_directives=_MASTER_SYS,
        name=agent_config.get("name", "Agent IA"),
        description=agent_config.get("system_prompt", "Tu es un assistant polyvalent."),
        tools_list=tools_list or "  (aucun outil disponible)",
    )


async def llm_call(messages: list, model: str = None, temperature: float = 0.7,
                   timeout: float = 0.0, impose: str = "") -> str:
    """Appel modèle NON bloquant et BORNÉ dans le temps.

    Dernier filet de sécurité : même si un SDK ignorait son propre délai, l'itération
    ReAct rend la main au lieu de laisser Nova « réfléchir » indéfiniment. Le thread
    parti en arrière-plan finira par expirer tout seul (délais côté llm/client.py).

    `timeout` permet de resserrer le délai quand on est DÉJÀ en retard (synthèse de
    dernière minute) : rallonger l'attente à ce moment-là ne ferait qu'aggraver le retard.
    """
    from llm.client import chat, TIMEOUT_CHAINE, DERNIER, executer_et_capturer
    loop = asyncio.get_running_loop()
    limite = timeout if timeout > 0 else TIMEOUT_CHAINE + 20.0
    fut = loop.run_in_executor(
        None, lambda: executer_et_capturer(chat, messages, temperature=temperature,
                                           impose=impose))
    try:
        sortie, modele = await asyncio.wait_for(fut, timeout=limite)
    except asyncio.TimeoutError:
        raise TimeoutError(f"aucun modèle n'a répondu en {int(limite)} s") from None
    if modele:                      # pour que l'UI puisse dire QUI a répondu
        DERNIER.set(modele)
    return sortie


async def _off(fn, *args, **kwargs):
    """Exécute une fonction BLOQUANTE dans un thread.

    ⚠️ Sans ça, un appel réseau synchrone (recherche web, outil Composio, mémoire
    vectorielle, reformulation LLM) lancé depuis une coroutine gèle TOUTE la boucle
    asyncio : plus aucun octet SSE n'est envoyé et l'utilisateur voit Nova
    « réfléchir » indéfiniment sans jamais recevoir de réponse.
    """
    import functools
    from llm.client import DERNIER, executer_et_capturer
    loop = asyncio.get_running_loop()
    res, modele = await loop.run_in_executor(
        None, functools.partial(executer_et_capturer, fn, *args, **kwargs))
    if modele:                      # le travail a appelé un modèle : on ramène son nom ici
        DERNIER.set(modele)
    return res


# Délai de la synthèse de dernière minute. Court par construction : on a déjà dépassé
# l'échéance, rallonger l'attente ne ferait qu'aggraver le retard côté utilisateur.
_SYNTHESE_TIMEOUT = 25.0


def _cle_requete(q: str) -> str:
    """Empreinte d'une requête de recherche, pour reconnaître deux demandes équivalentes.

    « actu tech du jour » et « Actu tech jour ! » donnent la même clé → la seconde
    recherche réutilise le résultat de la première au lieu de repayer le réseau.
    """
    mots = sorted(set(re.sub(r"[^\wÀ-ÿ\s]", " ", (q or "").lower()).split()))
    return " ".join(m for m in mots if len(m) > 2)


# Au-delà, on force la conclusion : le modèle relançait des recherches en boucle et
# consommait tout le temps imparti sans jamais rédiger.
MAX_RECHERCHES = 2


def apercu(texte: str, n: int = 140) -> str:
    """Extrait court destiné à l'AFFICHAGE, qui ne casse rien en chemin.

    Couper bêtement à N caractères laissait des marqueurs Markdown orphelins — «  _01net
    · 20 » ouvrait une italique jamais refermée — et tranchait les liens en deux
    (« https://01 »). On coupe donc sur une frontière de mot, on jette un lien entamé,
    et on rééquilibre les marqueurs.
    """
    t = (texte or "").strip()
    if len(t) <= n:
        return t
    coupe = t[:n]
    # Reculer jusqu'à la fin d'un mot (sauf si ça ampute plus de la moitié)
    i = max(coupe.rfind(" "), coupe.rfind("\n"))
    if i > n * 0.5:
        coupe = coupe[:i]
    # Un lien entamé ne mène nulle part : on le retire entièrement
    coupe = re.sub(r"\s*https?://\S*$", "", coupe)
    # Marqueurs restés ouverts : on les referme proprement plutôt que de les laisser
    for marque in ("```", "**", "`", "_"):
        if coupe.count(marque) % 2:
            j = coupe.rfind(marque)
            # Court fragment après le marqueur → on jette ; sinon on referme
            coupe = coupe[:j].rstrip() if len(coupe) - j <= 3 else coupe + marque
    return coupe.rstrip() + "…"


def _params_outil(action: str, params: dict) -> dict:
    """Complète les paramètres d'un outil que le modèle n'a pas su renseigner.

    ⚠️ Le modèle ne connaît QUE la description des plugins, jamais leurs paramètres :
    il ne peut donc pas fournir « mode ». Résultat, seule la recherche FORCÉE partait en
    mode actualité ; celles qu'il lançait ensuite repartaient en mode web et sautaient
    entièrement les flux RSS — d'où des communiqués de presse rendus comme actualité.
    On ne force le mode actualité que si SA requête est elle-même une demande d'actu :
    une relance ciblée (« prix Nintendo Switch 2 ») doit rester une recherche web.
    """
    p = dict(params or {})
    if action == "search_web" and "mode" not in p and veut_actualite(str(p.get("query", ""))):
        p["mode"] = "news"
    return p


# Les modèles « raisonneurs » (DeepSeek-R1, Qwen, GLM, Nemotron… nombreux chez NVIDIA)
# écrivent leur brouillon dans un bloc <think>. C'est leur cuisine interne, pas la réponse.
_RE_THINK = re.compile(
    r"<\s*(think|thinking|reasoning|scratchpad|antthinking)\s*>.*?<\s*/\s*\1\s*>",
    re.S | re.I)
_RE_THINK_OUVERT = re.compile(r"<\s*(think|thinking|reasoning|scratchpad)\s*>.*$", re.S | re.I)


def sans_raisonnement(sortie: str) -> str:
    """Retire le brouillon interne des modèles raisonneurs.

    Constaté en production : Nova affichait « <think> L'utilisateur demande un résumé…
    Je vais synthétiser cela en quelques phrases </think> » AVANT sa réponse. C'est le
    monologue interne du modèle — illisible, et il révèle la mécanique au lieu du résultat.
    """
    t = (sortie or "")
    t = _RE_THINK.sub("", t)
    # Bloc ouvert jamais refermé (réponse coupée en cours de route) : on jette la fin,
    # sinon l'utilisateur voit un brouillon tronqué.
    if re.search(r"<\s*(think|thinking|reasoning|scratchpad)\s*>", t, re.I):
        t = _RE_THINK_OUVERT.sub("", t)
    return t.strip()


class FiltreRaisonnement:
    """Retire le brouillon <think> AU FIL DU FLUX, sans jamais afficher un demi-tag.

    En streaming on ne peut pas attendre la fin pour nettoyer : chaque token part à
    l'écran. Ce filtre retient le texte qui pourrait être le début d'une balise, et
    n'émet rien tant qu'on est à l'intérieur du brouillon.
    """
    OUVRE = re.compile(r"<\s*(think|thinking|reasoning|scratchpad)\s*>", re.I)
    FERME = re.compile(r"<\s*/\s*(think|thinking|reasoning|scratchpad)\s*>", re.I)
    _SUSPECT = re.compile(r"<[^>]*$")           # balise peut-être coupée en deux tokens

    def __init__(self):
        self.tampon = ""
        self.dedans = False

    def __call__(self, morceau: str) -> str:
        self.tampon += morceau or ""
        sortie = []
        while True:
            if self.dedans:
                m = self.FERME.search(self.tampon)
                if not m:
                    # On reste dans le brouillon : on garde juste de quoi voir la fermeture.
                    self.tampon = self.tampon[-40:]
                    return "".join(sortie)
                self.tampon = self.tampon[m.end():]
                self.dedans = False
                continue
            m = self.OUVRE.search(self.tampon)
            if m:
                sortie.append(self.tampon[:m.start()])
                self.tampon = self.tampon[m.end():]
                self.dedans = True
                continue
            # Rien d'ouvert : on émet tout, sauf une balise possiblement coupée.
            garde = self._SUSPECT.search(self.tampon)
            coupe = garde.start() if garde else len(self.tampon)
            sortie.append(self.tampon[:coupe])
            self.tampon = self.tampon[coupe:]
            return "".join(sortie)

    def reste(self) -> str:
        """Ce qui n'a pas encore été émis à la fin du flux."""
        if self.dedans:
            return ""                            # brouillon jamais refermé : on le jette
        r, self.tampon = self.tampon, ""
        return r


def _texte_lisible(sortie: str) -> str:
    """Ne montre à l'utilisateur que de la prose — jamais le protocole interne.

    Quand l'échéance tombait alors que le modèle réclamait encore un outil, sa réponse
    (« THOUGHT: … ACTION: search_web … ») était affichée telle quelle dans le chat.
    """
    t = sans_raisonnement(sortie)
    if re.search(r"^\s*(ACTION|PARAMS)\s*:", t, re.M | re.I):
        return ""                                   # le modèle boucle encore : inutilisable
    t = re.sub(r"^\s*(THOUGHT|FINAL)\s*:\s*", "", t, flags=re.M | re.I).strip()
    return t


def _erreur_lisible(e: Exception) -> str:
    """Dernier recours : dire ce qui se passe et QUOI FAIRE, sans jargon technique."""
    t = str(e)
    if "limite" in t.lower() or "rate" in t.lower() or "429" in t:
        return ("⏳ Tous mes modèles gratuits sont à leur limite en ce moment. "
                "Réessaie dans une minute — ça se débloque tout seul.")
    manque = []
    for nom, cle, ou in (("Groq", "GROQ_API_KEY", "console.groq.com"),
                         ("Gemini", "GEMINI_API_KEY", "aistudio.google.com")):
        if cle.lower().split("_")[0] in t.lower():
            manque.append(f"**{nom}** ({ou})")
    aide = " ou ".join(manque) if manque else "**Groq** (console.groq.com)"
    return ("🔌 Je n'ai plus aucun modèle disponible pour rédiger une réponse.\n\n"
            f"Le plus simple : régénère une clé gratuite {aide} et mets-la dans les "
            "variables Render.\n\n_Détail technique :_\n```\n" + t[:400] + "\n```")


def _repli_observations(observations: list, task: str = "", raison: str = "delai") -> str:
    """Réponse de secours bâtie sur ce que les outils ont RÉELLEMENT rapporté.

    Sans ça, un dépassement de délai effaçait des résultats de recherche pourtant valides
    et n'affichait qu'une excuse : Nova avait trouvé Le Monde, Frandroid… et répondait
    « reformule ta question ». Ici on rend les trouvailles telles quelles, en disant
    honnêtement que la mise en forme n'a pas pu être faite.
    """
    utiles, vues = [], set()
    for o in observations:
        # Un message d'ERREUR ou de mode d'emploi n'est pas une trouvaille : il s'affichait
        # sous « voici ce que j'ai trouvé, sources à l'appui », ce qui était absurde.
        if not o or not o.strip() or o.lstrip().startswith(("[Self-heal]", "⚠️", "❌", "🔑")):
            continue
        # Deux recherches proches ramènent souvent les MÊMES pages : ne pas les répéter.
        empreinte = re.sub(r"\s+", " ", o)[:400]
        if empreinte in vues:
            continue
        vues.add(empreinte)
        utiles.append(o.split("\n\n[SYSTÈME]")[0].strip())
    if not utiles:
        return ""
    corps = "\n\n".join(utiles[-2:])[:2600]
    # Le motif compte : « pas eu le temps » et « plus aucun modèle » n'appellent pas la
    # même réaction de ta part.
    entete = ("⏱️ Je n'ai pas eu le temps de rédiger la synthèse, mais **voici ce que j'ai "
              "trouvé** — les sources sont réelles et vérifiables :"
              if raison == "delai" else
              "🔌 Aucun modèle n'est disponible pour rédiger, mais **la recherche a marché** — "
              "voici ce que j'ai trouvé, sources à l'appui :")
    pied = ("\n\n_Redemande-moi de résumer ces résultats si tu veux une synthèse rédigée._"
            if raison == "delai" else
            "\n\n_Réessaie dans un moment : dès qu'un modèle répond, je te rédige la synthèse._")
    return entete + "\n\n" + corps + pied


async def _remember_safe(mem, agent_id: str, texte: str) -> None:
    """Mémorise la réponse sans jamais bloquer ni faire échouer le flux."""
    try:
        await _off(mem.remember, agent_id, "assistant", texte)
    except Exception as e:
        logger.warning(f"mem.remember ignoré: {e}")


def _temperature_for_role(agent_config: dict) -> float:
    """Température adaptée au rôle. Factuel/finance = déterministe pour limiter les hallucinations."""
    role = (agent_config.get("role") or "").lower()
    if role in ("finance_analyst", "crypto_analyst") or "finance" in role:
        return 0.2
    if agent_config.get("force_search"):
        return 0.25  # questions factuelles/actu → basse température = moins d'inventions
    return 0.7


def parse_response(text: str) -> tuple:
    """Extrait (action, params, final) depuis la réponse LLM."""
    # ⚠️ Le brouillon <think> est retiré AVANT toute analyse : un « ACTION: search_web »
    # écrit dans le monologue interne du modèle (« je pourrais chercher sur le web… »)
    # déclencherait sinon un vrai appel d'outil qu'il n'a jamais demandé.
    text = sans_raisonnement(text)
    # FINAL
    final_m = re.search(r"FINAL:\s*(.+)", text, re.DOTALL | re.IGNORECASE)
    if final_m:
        return None, None, final_m.group(1).strip()

    # ACTION + PARAMS
    # ⚠️ Le modèle DÉCORE souvent le nom de l'outil — et c'est notre gabarit qui le lui
    # apprend : il écrit lui-même « ACTION: [nom_exact_de_l_outil] ». Quand le modèle
    # recopie la convention (« ACTION: [search_web] », « `search_web` », « **ACTION:** »),
    # `\w+` ne matchait plus rien, on tombait dans le fourre-tout du bas, et le PROTOCOLE
    # BRUT était affiché comme réponse — l'outil n'étant jamais lancé.
    # Noter l'asymétrie qui avait laissé passer le bug : FINAL utilisait déjà `(.+)`,
    # donc tolérait toute décoration ; ACTION non.
    action_m = re.search(r"ACTION\s*\**\s*:\s*[\[`\"'*\s]*([A-Za-z_][\w.-]*)",
                         text, re.IGNORECASE)
    params_m = re.search(r"PARAMS\s*\**\s*:\s*[`\s]*(\{.+?\})", text,
                         re.DOTALL | re.IGNORECASE)

    if action_m:
        action = action_m.group(1).strip()
        params = {}
        if params_m:
            try:
                params = json.loads(params_m.group(1))
            except Exception:
                try:
                    params = json.loads(params_m.group(1).replace("'", '"'))
                except Exception:
                    pass
        return action, params, None

    # Si aucun format reconnu → traiter comme réponse finale.
    # ⚠️ MAIS un texte qui contient encore ACTION:/PARAMS: n'est PAS une réponse : c'est
    # du protocole que personne n'a su lire. L'afficher revenait à montrer la tuyauterie
    # à l'utilisateur. _texte_lisible sait déjà le reconnaître — il n'était simplement
    # jamais appelé sur ce chemin.
    brut = text.strip()
    if brut and not _texte_lisible(brut):
        return None, None, None
    return None, None, brut


_STUB_KEYWORDS = (
    "attendre", "en cours", "analyse en cours", "à analyser", "dépend",
    "consulter un professionnel", "je ne peux pas", "indisponible",
    "je n'ai pas accès", "données manquantes", "impossible de",
    "attentes les résultats", "attends les résultats",
)

def _is_stub_answer(text: str, tool_calls_made: int, needs_tools: bool = False) -> bool:
    """Détecte une réponse VRAIMENT paresseuse : le LLM se défausse alors qu'un outil était requis.

    ⚠️ Historique : l'ancienne heuristique « réponse < 400 caractères sans chiffre = stub »
    forçait un appel d'outil sur TOUTE réponse courte, ce qui poussait le modèle à produire
    des pavés remplis de chiffres… inventés. Supprimée : une réponse courte est souvent
    la bonne réponse (« salut », « ok », une remarque personnelle).
    """
    if tool_calls_made > 0 or not needs_tools:
        return False
    t = text.lower()
    return any(kw in t for kw in _STUB_KEYWORDS)


async def run_agent(
    task: str,
    agent_config: dict,
    agent_id: str = "default",
    plugin_loader=None,
    memory_manager=None,
) -> dict:
    from plugins import get_loader
    from memory import get_memory
    from agent.self_heal import safe_tool_call, health_monitor
    from memory.summarizer import summarize_messages

    loader = plugin_loader or get_loader()
    mem = memory_manager or get_memory()
    system = build_system(agent_config, loader.list_all())
    temperature = _temperature_for_role(agent_config)

    # Auto-résumé si historique long (mémoire non fatale)
    try:
        if mem.should_summarize(agent_id):
            recent = mem.recall_recent(agent_id, limit=config.SUMMARY_THRESHOLD)
            summary = await summarize_messages(recent)
            mem.cache_summary(agent_id, summary)
    except Exception as e:
        logger.warning(f"Auto-résumé/mémoire ignoré: {e}")

    # Injecte les leçons apprises si disponibles
    try:
        from agent.self_improve import get_improvement_context
        domain = agent_config.get("role", "general")
        lessons = get_improvement_context(domain=domain, max_lessons=4)
        if lessons:
            system = system + f"\n\n{lessons}"
    except Exception:
        pass

    try:
        context = await _off(mem.build_context, agent_id, task, recent_limit=6)
    except Exception as e:
        logger.warning(f"build_context ignoré: {e}")
        context = ""
    messages = [{"role": "system", "content": system}]
    if context:
        messages.append({"role": "assistant", "content": f"[Contexte mémoriel]\n{context}"})

    # Rappel outil UNIQUEMENT si la question réclame des données réelles (factuel/temps réel).
    # Auparavant ce rappel était injecté sur CHAQUE message → l'agent dégainait un outil même
    # pour « j'ai 17 ans », ce qui produisait des rapports hors-sujet.
    required_tools = agent_config.get("tools") or []
    task_msg = task
    if required_tools and agent_config.get("force_search"):
        task_msg += (
            f"\n\n[INSTRUCTION SYSTÈME: question factuelle → utilise tes outils pour obtenir des "
            f"données réelles avant de répondre. Outil suggéré : {required_tools[0]}]"
        )
    messages.append({"role": "user", "content": task_msg})
    try:
        await _off(mem.remember, agent_id, "user", task)
    except Exception as e:
        logger.warning(f"mem.remember ignoré: {e}")

    steps = []
    tool_calls_made = 0
    stub_retries = 0
    # Un outil n'est "requis" que si la question est factuelle/temps réel (force_search).
    needs_tools = bool(agent_config.get("force_search"))

    # Forçage déterministe de search_web sur les questions factuelles (idem run_agent_stream)
    observations, deja_cherche = [], {}
    _fin_pre = _tm.monotonic() + float(getattr(config, "AGENT_TIMEOUT", 75))
    if agent_config.get("force_search") and "search_web" in required_tools:
        try:
            _q = await _off(search_query, task)
            obs = await _off(safe_tool_call, loader, "search_web",
                             {"query": _q, "mode": "news" if veut_actualite(task) else "web"},
                             "", _fin_pre)
            observations.append(obs)
            deja_cherche[_cle_requete(_q)] = obs
            steps.append({"type": "action", "tool": "search_web", "params": {"query": _q}})
            steps.append({"type": "observation", "tool": "search_web", "result": apercu(obs, 400)})
            messages.append({"role": "assistant", "content":
                "ACTION: search_web\nPARAMS: " + json.dumps({"query": _q}, ensure_ascii=False)})
            messages.append({"role": "user", "content": (
                f"OBSERVATION [search_web]: {obs[:1400]}\n\n"
                "Utilise UNIQUEMENT ces résultats réels pour répondre, en citant leurs sources.")})
            tool_calls_made += 1
        except Exception as e:
            logger.warning(f"[force_search] échec: {e}")
    MAX_STUB_RETRIES = 2

    # ⏱️ Même échéance que run_agent_stream, armée AVANT la recherche forcée.
    _fin = _fin_pre

    iteration = 0
    while iteration < config.MAX_ITERATIONS:
        if _tm.monotonic() > _fin:
            logger.warning(f"[core] échéance atteinte après {iteration} itération(s) → synthèse immédiate")
            messages.append({"role": "user", "content": (
                "⏱️ Temps imparti atteint. Donne MAINTENANT ta réponse FINAL avec ce que tu as déjà "
                "trouvé. Si l'information exacte manque, dis-le franchement. Ne lance plus aucune recherche.")})
            try:
                dernier = await llm_call(messages, temperature=temperature, timeout=_SYNTHESE_TIMEOUT,
                                        impose=agent_config.get('fournisseur', ''))
                _a, _p, fin_txt = parse_response(dernier)
                # Si le modèle réclame ENCORE un outil, sa sortie n'est pas une réponse :
                # on la jette au profit des trouvailles réelles.
                rep = (fin_txt or "").strip() if fin_txt else _texte_lisible(dernier)
            except Exception as e:
                logger.warning(f"[core] synthèse de dernière minute impossible : {str(e)[:120]}")
                rep = ""
            rep = rep or _repli_observations(observations, task) or (
                "⏱️ La recherche a pris trop de temps et n'a rien rapporté d'exploitable. "
                "Reformule ta question ou précise-la.")
            await _remember_safe(mem, agent_id, rep[:350])
            return {"answer": rep, "steps": steps, "iterations": iteration + 1}
        try:
            llm_out = await llm_call(messages, temperature=temperature,
                                     impose=agent_config.get('fournisseur', ''))
        except Exception as e:
            secours = _repli_observations(observations, task, "sans_modele")
            if secours:
                logger.warning(f"[core] aucun modèle → on rend les trouvailles ({str(e)[:90]})")
                await _remember_safe(mem, agent_id, secours[:350])
                return {"answer": secours, "steps": steps, "iterations": iteration}
            logger.error(f"LLM indisponible: {e}")
            return {"answer": _erreur_lisible(e), "steps": steps,
                    "iterations": iteration, "error": str(e)}

        step = {"iteration": iteration + 1, "llm_output": llm_out}
        action, params, final = parse_response(llm_out)

        # ── Détection réponse paresseuse (aucun outil appelé, réponse vague) ──
        response_text = final or llm_out
        if (final or (not action)) and required_tools and stub_retries < MAX_STUB_RETRIES:
            if _is_stub_answer(response_text, tool_calls_made, needs_tools):
                stub_retries += 1
                first_tool = required_tools[0]
                logger.warning(f"[core] Réponse stub iter {iteration+1} — forçage outil '{first_tool}' (retry {stub_retries}/{MAX_STUB_RETRIES})")
                messages.append({"role": "assistant", "content": llm_out})
                messages.append({"role": "user", "content": (
                    f"Tu t'es défaussé alors qu'un outil pouvait répondre.\n"
                    f"Utilise '{first_tool}' avec des paramètres adaptés à la question :\n"
                    f"THOUGHT: je récupère les données réelles avec {first_tool}\n"
                    f"ACTION: {first_tool}\n"
                    f"PARAMS: {{...}}"
                )})
                # Ne pas incrémenter iteration — rejouer sans compter comme une itération normale
                continue

        if final:
            steps.append(step)
            await _remember_safe(mem, agent_id, final[:350])
            return {"answer": final, "steps": steps, "iterations": iteration + 1}

        if action:
            cle = _cle_requete((params or {}).get("query", "")) if action == "search_web" else ""
            if cle and cle in deja_cherche:
                observation = deja_cherche[cle]
                logger.info("[core] recherche identique déjà faite → résultat réutilisé")
            elif cle and len(deja_cherche) >= MAX_RECHERCHES:
                # Le modèle relançait des recherches jusqu'à épuiser le temps imparti sans
                # jamais rédiger. On lui rend ce qu'il a déjà et on lui coupe l'échappatoire.
                observation = ("\n\n".join(deja_cherche.values())[:1200] +
                               "\n\n[SYSTÈME] Tu as déjà lancé "
                               f"{len(deja_cherche)} recherches. N'en lance plus AUCUNE : "
                               "réponds MAINTENANT avec FINAL: à partir de ces résultats.")
                logger.info("[core] plafond de recherches atteint → conclusion forcée")
            else:
                observation = await _off(safe_tool_call, loader, action,
                                         _params_outil(action, params), "", _fin)
                if cle:
                    deja_cherche[cle] = observation
            observations.append(observation)
            health_monitor.record(action, "Erreur" not in observation)
            tool_calls_made += 1
            step["action"] = action
            step["params"] = params
            step["observation"] = observation[:500]
            steps.append(step)
            messages.append({"role": "assistant", "content": llm_out})
            messages.append({"role": "user", "content": (
                f"OBSERVATION [{action}]: {observation[:1200]}\n\n"
                f"Continue. Si tu as les informations nécessaires, donne ta réponse FINAL "
                f"— en te basant UNIQUEMENT sur les observations réelles ci-dessus :"
            )})
        else:
            steps.append(step)
            propre = _texte_lisible(llm_out) or _repli_observations(observations, task) or llm_out
            await _remember_safe(mem, agent_id, propre)
            return {"answer": propre, "steps": steps, "iterations": iteration + 1}

        iteration += 1

    last = _repli_observations(observations, task) or (
        steps[-1].get("llm_output", "Limite d'itérations atteinte.") if steps
        else "Limite d'itérations atteinte.")
    await _remember_safe(mem, agent_id, last)
    return {"answer": last, "steps": steps, "iterations": config.MAX_ITERATIONS}


def search_query(task: str) -> str:
    """Transforme la demande en VRAIE requête de recherche (mots-clés), pas la phrase entière.

    Envoyer « Quand se fait la rentrée pour les premières année à Pau à l'uppa en eco gestion ? »
    à un moteur donne de mauvais résultats : on en extrait « rentrée UPPA Pau licence économie
    gestion 2026 date ».
    """
    from llm.client import chat
    from datetime import datetime
    t = (task or "").strip()
    # ⚠️ Le modèle ignore la date du jour et met l'année de son entraînement (« 2024 »).
    # On la lui donne explicitement, sinon les recherches d'actualité sont périmées.
    auj = datetime.now()
    actu = veut_actualite(t)
    # ⚠️ En mode actualité, la date est CONTRE-PRODUCTIVE : le moteur remonte alors les
    # pages qui contiennent « 20 août 2026 » (communiqués, trading updates) au lieu des
    # articles du jour, que les flux et moteurs d'actu trient déjà par fraîcheur.
    # La consigne donnée au modèle doit donc changer selon le cas — sinon il rajoute la
    # date lui-même et le garde-fou de requete_simple() est contourné.
    regle_date = (
        "• N'ajoute NI date NI année : la fraîcheur est déjà gérée par le moteur d'actualité.\n"
        if actu else
        f"• Actualité DU JOUR (« aujourd'hui », « du jour », « ce matin ») → ajoute la date "
        f"précise « {_JOURS_COURT(auj)} » pour ne pas ramener des articles vieux de plusieurs mois.\n"
        f"• Autre information récente → ajoute simplement « {auj.year} ».\n")
    try:
        out = chat([
            {"role": "system", "content": (
                f"Nous sommes le {auj.strftime('%d/%m/%Y')}. L'année en cours est {auj.year}.\n"
                "Transforme la demande en une REQUÊTE de moteur de recherche efficace.\n"
                "RÈGLES : 3 à 8 mots-clés, pas de question, pas de mots vides (le, la, pour, quand…), "
                "garde les noms propres et sigles.\n"
                + regle_date +
                "• JAMAIS une année passée. Réponds UNIQUEMENT par la requête, sans guillemets.")},
            {"role": "user", "content": t[:300]},
        ], temperature=0.2) or ""
        q = out.strip().strip('"«».\n').split("\n")[0]
        # Garde-fou : le modèle glisse parfois une année périmée (2023/2024/2025).
        # Si l'utilisateur n'a PAS demandé cette année-là, on la remplace par l'année courante.
        for vieille in range(2020, auj.year):
            if str(vieille) in q and str(vieille) not in t:
                q = q.replace(str(vieille), str(auj.year))
        if actu:
            # Le modèle désobéit parfois : on retire la date quoi qu'il arrive.
            q = re.sub(r"\b\d{1,2}\s+(?:" + "|".join(_MOIS) + r")\s+\d{4}\b", "", q, flags=re.I)
            q = re.sub(r"\b(?:20\d{2})\b", "", q)
            q = re.sub(r"\s{2,}", " ", q).strip(" ,-—")
        if 3 <= len(q) <= 120:
            return q
    except Exception as e:
        logger.warning(f"[search_query] reformulation ignorée: {e}")
    return requete_simple(t, pour_actu=veut_actualite(t))


# Verbes de COMMANDE : ils décrivent ce que Nova doit faire, pas ce qu'il faut chercher.
# « Résume l'actu tech du jour » cherché tel quel ramenait un podcast intitulé
# « L'Actu Tech, chaque jour Patrick résume… » au lieu de l'actualité elle-même.
_VERBES_DEMANDE = {
    "résume", "resume", "résumé", "resumé", "résumes", "donne", "donnes", "donner",
    "explique", "expliques", "expliquer", "dis", "dit", "dire", "montre", "montres",
    "cherche", "cherches", "recherche", "recherches", "trouve", "trouves", "raconte",
    "parle", "parles", "liste", "listes", "indique", "détaille", "detaille", "précise",
    "presente", "présente", "fais", "fait", "faire", "peux", "peut", "pourrais",
    "voudrais", "veux", "aimerais", "sais", "connais", "penses", "pense",
    # Verbes d'accès : ils décrivent le geste, jamais ce qu'on cherche.
    # Sans eux, « lit mon fichier pea » cherchait « lit pea » et ne trouvait rien.
    "lis", "lit", "lire", "ouvre", "ouvrir", "ouvres", "accede", "accède", "accéder",
    "accedes", "accèdes", "consulte", "consulter", "consultes", "va", "vas", "aller",
    "regarde", "regarder", "regardes", "affiche", "afficher", "affiches",
    "récupère", "recupere", "récupérer", "recuperer", "vérifie", "verifie",
}
_STOP_REQUETE = {
    "quand", "comment", "pourquoi", "est", "ce", "que", "qui", "quoi", "le", "la", "les",
    "un", "une", "des", "de", "du", "pour", "à", "a", "en", "se", "dans", "sur", "au", "aux",
    "mon", "ma", "mes", "je", "tu", "il", "elle", "on", "nous", "vous", "moi", "me", "te",
    "stp", "svp", "quel", "quelle", "quels", "quelles", "y", "et", "ou", "où", "son", "sa",
    "plait", "plaît", "merci", "please", "the", "of", "toi", "lui", "leur", "d", "l", "s",
}
# Ces mots signalent qu'on veut du RÉCENT → il faut dater la requête, sinon le moteur
# ramène des pages génériques sans rapport avec la journée en cours.
_MOTS_DU_JOUR = ("aujourd'hui", "aujourdhui", "du jour", "ce matin", "ce soir", "cette nuit",
                 "maintenant", "actuellement", "en ce moment", "de la journée")
_MOTS_RECENT = ("actu", "actus", "actualité", "actualite", "actualités", "actualites", "news",
                "nouvelles", "quoi de neuf", "dernières", "dernieres", "récent", "recent",
                "cette semaine", "en direct", "live")


# Tournures de politesse / de demande à retirer AVANT le découpage en mots : découpées,
# elles laissaient des résidus (« quoi de neuf » → « neuf »).
_TOURNURES = ("quoi de neuf", "est ce que", "est-ce que", "s'il te plait", "s'il te plaît",
              "s'il vous plait", "peux tu", "peux-tu", "pourrais tu", "pourrais-tu",
              "c'est quoi", "qu'est ce que", "qu'est-ce que", "parle moi de", "parle-moi de",
              "dis moi", "dis-moi", "donne moi", "donne-moi", "montre moi", "montre-moi")


def _normalise(t: str) -> str:
    """Apostrophes typographiques, élisions détachées : tout ramené à une forme unique."""
    t = (t or "").replace("’", "'").replace("ʼ", "'")
    t = re.sub(r"\baujourd\s+hui\b", "aujourd'hui", t, flags=re.I)
    return t


# Une demande qui NOMME un sujet précis n'est pas une demande d'actualité générale.
# « tu penses quoi d'acheter 2CRSI maintenant ? » contient « maintenant » : Nova basculait
# donc en mode ACTUALITÉ et rendait… les titres politiques du jour. Le mot situait le
# moment de la décision, pas le sujet de la recherche.
_SUJETS_PRECIS = ("action", "actions", "titre", "bourse", "cours de", "cotation", "ticker",
                  "etf", "pea", "crypto", "bitcoin", "dividende", "capitalisation",
                  "acheter", "acheté", "achete", "vendre", "investir", "placement",
                  "météo", "meteo", "recette", "traduis", "traduction", "définition",
                  "definition", "calcule", "combien", "itinéraire", "itineraire")


def veut_actualite(task: str) -> bool:
    """La demande porte-t-elle sur l'actualité ? (→ requête datée, résultats récents)"""
    m = _normalise(task).lower()
    # Un mot d'ACTUALITÉ explicite (« actu », « news », « quoi de neuf ») tranche seul.
    if any(k in m for k in _MOTS_RECENT):
        return True
    # « maintenant », « aujourd'hui »… ne suffisent pas : ils situent le MOMENT, pas le
    # sujet. Ils ne basculent en mode actualité que si la demande ne nomme rien de précis.
    if any(k in m for k in _MOTS_DU_JOUR):
        return not any(k in m for k in _SUJETS_PRECIS)
    return False


def requete_simple(task: str, pour_actu: bool = False) -> str:
    """Requête de recherche construite SANS modèle — donc toujours disponible.

    Le repli précédent se contentait de retirer quelques mots vides : « Résume l'actu tech
    du jour » devenait « Résume l'actu tech jour », qui ramenait un podcast. Ici on retire
    les verbes de commande ET on ajoute la date quand la demande porte sur l'actualité.
    """
    from datetime import datetime
    t = _normalise(task).strip()
    if not t:
        return ""
    auj = datetime.now()
    bas = t.lower()
    nettoye = t                                   # on garde la casse : « UPPA », « IA »…
    for tour in _TOURNURES:                       # retirées en entier, pas mot à mot
        nettoye = re.sub(re.escape(tour), " ", nettoye, flags=re.I)
    mots = []
    for w in re.sub(r"[^\w\sÀ-ÿ'-]", " ", nettoye).split():
        w = re.sub(r"^(?:[ldnjmtsc]|qu)'", "", w, flags=re.I)  # l'actu → actu, d'IA → IA
        nu = w.lower().strip("'-")
        if not nu or nu in _STOP_REQUETE or nu in _VERBES_DEMANDE:
            continue
        # « aujourd'hui », « jour »… sont remplacés par une vraie date, plus bas
        if nu in ("jour", "journée", "journee", "aujourd'hui", "aujourdhui", "matin",
                  "soir", "moment", "nuit"):
            continue
        mots.append("actualité" if nu in ("actu", "actus") else w)
        if len(mots) >= 8:
            break
    q = " ".join(mots).strip()
    # ⚠️ La date n'est ajoutée QUE pour une recherche web classique. En mode actualité
    # elle est contre-productive : le moteur remonte alors les pages qui CONTIENNENT
    # « 20 août 2026 » — communiqués de presse, « trading updates » — au lieu des
    # articles du jour, que les flux et moteurs d'actu trient déjà par fraîcheur.
    if pour_actu:
        return q[:120] or t[:120]
    if any(k in bas for k in _MOTS_DU_JOUR) or "du jour" in bas:
        q = f"{q} {_JOURS_COURT(auj)}".strip()          # « 19 août 2026 »
    elif any(k in bas for k in _MOTS_RECENT):
        q = f"{q} {auj.year}".strip()
    return q[:120] or t[:120]


def _extract_thought(llm_out: str) -> str:
    """Extrait la section THOUGHT: d'une sortie LLM."""
    m = re.search(r"THOUGHT:\s*(.+?)(?=\n(?:ACTION|FINAL|PARAMS):|$)", llm_out, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip()[:200] if m else ""


async def run_agent_stream(
    task: str,
    agent_config: dict,
    agent_id: str = "default",
    plugin_loader=None,
    memory_manager=None,
):
    """
    Async generator — même logique que run_agent mais yield chaque étape ReAct.
    Permet d'afficher le raisonnement en temps réel dans l'UI.

    Yields dicts:
      {"type": "thought",      "text": str, "iteration": int}
      {"type": "action",       "tool": str, "params": dict, "iteration": int}
      {"type": "observation",  "tool": str, "result": str,  "iteration": int}
      {"type": "final",        "answer": str, "iterations": int}
    """
    from plugins import get_loader
    from memory import get_memory
    from agent.self_heal import safe_tool_call, health_monitor
    from memory.summarizer import summarize_messages

    loader = plugin_loader or get_loader()
    mem    = memory_manager or get_memory()
    system = build_system(agent_config, loader.list_all())
    temperature = _temperature_for_role(agent_config)

    try:
        if await _off(mem.should_summarize, agent_id):
            recent = await _off(mem.recall_recent, agent_id, limit=config.SUMMARY_THRESHOLD)
            summary = await summarize_messages(recent)
            await _off(mem.cache_summary, agent_id, summary)
    except Exception:
        pass

    try:
        from agent.self_improve import get_improvement_context
        domain  = agent_config.get("role", "general")
        lessons = get_improvement_context(domain=domain, max_lessons=4)
        if lessons:
            system = system + f"\n\n{lessons}"
    except Exception:
        pass

    try:
        context = await _off(mem.build_context, agent_id, task, recent_limit=6)
    except Exception as e:
        logger.warning(f"build_context ignoré: {e}")
        context = ""
    messages = [{"role": "system", "content": system}]
    if context:
        messages.append({"role": "assistant", "content": f"[Contexte mémoriel]\n{context}"})

    required_tools = agent_config.get("tools") or []
    task_msg = task
    if required_tools and agent_config.get("force_search"):
        task_msg += (
            f"\n\n[INSTRUCTION SYSTÈME: question factuelle → utilise tes outils pour des données "
            f"réelles avant de répondre. Outil suggéré : {required_tools[0]}]"
        )
    messages.append({"role": "user", "content": task_msg})
    try:
        await _off(mem.remember, agent_id, "user", task)
    except Exception as e:
        logger.warning(f"mem.remember ignoré: {e}")

    tool_calls_made = 0
    stub_retries    = 0
    needs_tools     = bool(agent_config.get("force_search"))

    # ⏱️ Échéance globale, armée AVANT la recherche forcée : sinon une recherche lente
    # consommait déjà plusieurs minutes avant même que le chrono ne démarre.
    _fin = _tm.monotonic() + float(getattr(config, "AGENT_TIMEOUT", 75))

    # ── FORÇAGE DÉTERMINISTE DE search_web pour les questions factuelles ──────
    # On exécute une VRAIE recherche DuckDuckGo AVANT le 1er appel LLM et on injecte
    # l'observation → le modèle répond sur des données réelles (avec vraies sources),
    # il ne peut plus halluciner ni exécuter un script Python à la place.
    # Tout ce que les outils rapportent : sert de réponse de secours si le temps manque
    # pour rédiger. Ces données sont réelles — les jeter serait absurde.
    observations, deja_cherche = [], {}

    if agent_config.get("force_search") and "search_web" in required_tools:
        try:
            _q = await _off(search_query, task)
            yield {"type": "action", "tool": "search_web", "params": {"query": _q}, "iteration": 0}
            _mode = "news" if veut_actualite(task) else "web"
            obs = await _off(safe_tool_call, loader, "search_web",
                             {"query": _q, "mode": _mode}, "", _fin)
            observations.append(obs)
            deja_cherche[_cle_requete(_q)] = obs
            yield {"type": "observation", "tool": "search_web", "result": apercu(obs, 400), "iteration": 0}
            messages.append({"role": "assistant", "content":
                "THOUGHT: recherche web pour données réelles\nACTION: search_web\n"
                "PARAMS: " + json.dumps({"query": _q, "mode": _mode}, ensure_ascii=False)})
            messages.append({"role": "user", "content": (
                f"OBSERVATION [search_web]: {obs[:1400]}\n\n"
                "Utilise UNIQUEMENT ces résultats réels pour répondre, en citant leurs sources. "
                "Toute source, DATE ou chiffre que tu cites DOIT apparaître mot pour mot dans ces "
                "résultats — interdiction absolue d'inventer un nom de média, une date ou un montant. "
                "Si l'info manque, dis clairement « je n'ai pas trouvé ».")})
            tool_calls_made += 1
        except Exception as e:
            logger.warning(f"[force_search] échec: {e}")

    for iteration in range(config.MAX_ITERATIONS):
        if _tm.monotonic() > _fin:
            logger.warning(f"[core] échéance atteinte après {iteration} itération(s) → synthèse immédiate")
            messages.append({"role": "user", "content": (
                "⏱️ Temps imparti atteint. Donne MAINTENANT ta réponse FINAL avec ce que tu as déjà "
                "trouvé. Si l'information exacte manque, dis-le franchement et donne le lien le plus "
                "pertinent des résultats. Ne lance plus aucune recherche.")})
            rep = ""
            try:
                # Délai resserré : on est DÉJÀ en retard, attendre encore 90 s serait pire.
                dernier = await llm_call(messages, temperature=temperature, timeout=_SYNTHESE_TIMEOUT,
                                        impose=agent_config.get('fournisseur', ''))
                _a, _p, fin_txt = parse_response(dernier)
                # Si le modèle réclame ENCORE un outil, sa sortie n'est pas une réponse :
                # on la jette au profit des trouvailles réelles.
                rep = (fin_txt or "").strip() if fin_txt else _texte_lisible(dernier)
            except Exception as e:
                logger.warning(f"[core] synthèse de dernière minute impossible : {str(e)[:120]}")
            # Le modèle n'a rien donné → on rend quand même les trouvailles réelles.
            rep = rep or _repli_observations(observations, task) or (
                "⏱️ La recherche a pris trop de temps et n'a rien rapporté d'exploitable. "
                "Reformule ta question ou précise-la.")
            await _remember_safe(mem, agent_id, rep[:350])
            yield {"type": "final", "answer": rep, "iterations": iteration + 1}
            return
        try:
            llm_out = await llm_call(messages, temperature=temperature,
                                     impose=agent_config.get('fournisseur', ''))
        except Exception as e:
            # ⚠️ Le filet de secours manquait ICI. Les outils avaient rapporté de VRAIES
            # données (articles, sources, liens) et Nova affichait quand même une erreur
            # brute. Aucun modèle ne répond ? On rend au moins ce qu'on a trouvé.
            secours = _repli_observations(observations, task, "sans_modele")
            if secours:
                logger.warning(f"[core] aucun modèle → on rend les trouvailles ({str(e)[:90]})")
                await _remember_safe(mem, agent_id, secours[:350])
                yield {"type": "final", "answer": secours, "iterations": iteration}
                return
            yield {"type": "final", "answer": _erreur_lisible(e), "iterations": iteration}
            return

        thought = _extract_thought(llm_out)
        action, params, final = parse_response(llm_out)

        # Stub detection
        response_text = final or llm_out
        if (final or not action) and required_tools and stub_retries < 2:
            if _is_stub_answer(response_text, tool_calls_made, needs_tools):
                stub_retries += 1
                first_tool = required_tools[0]
                messages.append({"role": "assistant", "content": llm_out})
                messages.append({"role": "user", "content": (
                    f"Un outil peut répondre. Utilise '{first_tool}' maintenant.\n"
                    f"THOUGHT: je récupère les données réelles\nACTION: {first_tool}\nPARAMS: {{}}"
                )})
                continue

        if thought:
            yield {"type": "thought", "text": thought, "iteration": iteration + 1}

        if final:
            await _remember_safe(mem, agent_id, final[:350])
            yield {"type": "final", "answer": final, "iterations": iteration + 1}
            return

        if action:
            yield {"type": "action", "tool": action, "params": params or {}, "iteration": iteration + 1}
            # Recherche déjà faite dans ce tour ? On rend le résultat mémorisé au lieu de
            # repayer 30 s de réseau. Le modèle relançait la MÊME requête qu'au forçage,
            # ce qui consommait à lui seul la moitié du temps imparti.
            cle = _cle_requete((params or {}).get("query", "")) if action == "search_web" else ""
            if cle and cle in deja_cherche:
                observation = deja_cherche[cle]
                logger.info("[core] recherche identique déjà faite → résultat réutilisé")
            elif cle and len(deja_cherche) >= MAX_RECHERCHES:
                # Le modèle relançait des recherches jusqu'à épuiser le temps imparti sans
                # jamais rédiger. On lui rend ce qu'il a déjà et on lui coupe l'échappatoire.
                observation = ("\n\n".join(deja_cherche.values())[:1200] +
                               "\n\n[SYSTÈME] Tu as déjà lancé "
                               f"{len(deja_cherche)} recherches. N'en lance plus AUCUNE : "
                               "réponds MAINTENANT avec FINAL: à partir de ces résultats.")
                logger.info("[core] plafond de recherches atteint → conclusion forcée")
            else:
                observation = await _off(safe_tool_call, loader, action,
                                         _params_outil(action, params), "", _fin)
                if cle:
                    deja_cherche[cle] = observation
            observations.append(observation)
            health_monitor.record(action, "Erreur" not in observation)
            tool_calls_made += 1
            yield {"type": "observation", "tool": action, "result": apercu(observation, 400), "iteration": iteration + 1}
            messages.append({"role": "assistant", "content": llm_out})
            messages.append({"role": "user", "content": (
                f"OBSERVATION [{action}]: {observation[:1200]}\n\n"
                "Continue. Si tu as toutes les données, donne ta réponse FINAL:"
            )})
        else:
            propre = _texte_lisible(llm_out) or _repli_observations(observations, task) or llm_out
            await _remember_safe(mem, agent_id, propre)
            yield {"type": "final", "answer": propre, "iterations": iteration + 1}
            return

    # Plafond d'itérations : là encore, on rend les trouvailles plutôt qu'un message vide.
    last = _repli_observations(observations, task) or \
        f"⚠️ Limite de {config.MAX_ITERATIONS} itérations atteinte."
    await _remember_safe(mem, agent_id, last)
    yield {"type": "final", "answer": last, "iterations": config.MAX_ITERATIONS}
