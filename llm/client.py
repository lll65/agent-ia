"""
Client LLM unifié — choisit automatiquement le provider selon le .env :
  GROQ_API_KEY      → Groq (gratuit, Llama 3.3 70B)
  XAI_API_KEY       → xAI Grok
  CEREBRAS_API_KEY  → Cerebras (gratuit, inference ultra-rapide, Llama 3.3 70B)
  GEMINI_API_KEY    → Google AI Studio (Gemini 2.5 Flash, gratuit, sans CB)
  sinon              → Ollama local

Fallback automatique : si Groq renvoie 429 (rate limit) et qu'une clé Cerebras
est configurée, la requête bascule automatiquement sur Cerebras (même format
OpenAI que Groq). Si les deux échouent, l'erreur est loggée clairement.
"""
import logging
import os
import threading
import time as _t
import re
from config import config

logger = logging.getLogger(__name__)


def _is_rate_limit(exc: Exception) -> bool:
    """Détecte un 429 / rate-limit, quel que soit le SDK."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text or "quota" in text


_MODELES_OK = {}   # fournisseur -> modèle qui a réellement fonctionné (mémorisé)
# ⚠️ Ce souvenir était DÉFINITIF. Quand les modèles préférés étaient momentanément à
# leur limite, Nova basculait sur un modèle de repli… et le gardait pour toujours.
# Vu en vrai : Groq répondant avec « allam-2-7b », un modèle arabophone de 7 milliards
# de paramètres, à un utilisateur qui parle français. Un rate-limit de 60 secondes
# condamnait donc la qualité pour le reste de la journée. Le repli est maintenant
# retenu 10 minutes, puis on redonne sa chance au modèle préféré.
_MODELES_OK_TS = {}
_REPLI_TTL = 600.0


def _modele_memorise(fournisseur: str) -> str:
    m = _MODELES_OK.get(fournisseur)
    if not m:
        return ""
    if _t.monotonic() - _MODELES_OK_TS.get(fournisseur, 0.0) > _REPLI_TTL:
        _MODELES_OK.pop(fournisseur, None)      # on retente le modèle préféré
        return ""
    return m


def _retenir_modele(fournisseur: str, modele: str) -> None:
    _MODELES_OK[fournisseur] = modele
    _MODELES_OK_TS[fournisseur] = _t.monotonic()


# Modèles à ne JAMAIS choisir tout seul : spécialisés dans une autre langue ou une
# autre tâche. Ils « fonctionnent » (donc l'auto-guérison les acceptait), mais la
# réponse est mauvaise — et c'est invisible, puisqu'il n'y a pas d'erreur.
_MODELES_INADAPTES = ("allam", "arabic", "-ar-", "saba", "jais",
                      "code-", "coder", "embed", "guard", "whisper", "tts",
                      "vision", "ocr", "rerank", "moderation", "compound", "agent")

# Familles connues pour bien répondre en français, dans l'ordre de préférence.
_FAMILLES_SURES = ("llama-3.3", "llama-3.1", "llama-4", "gpt-oss", "mixtral",
                   "mistral", "qwen", "gemma", "llama")


def _rang_modele(mid: str) -> int:
    """Plus c'est petit, mieux c'est. Un modèle inconnu passe après les familles sûres."""
    bas = (mid or "").lower()
    for i, f in enumerate(_FAMILLES_SURES):
        if f in bas:
            return i
    return len(_FAMILLES_SURES)
_MODELES_KO = {}   # (fournisseur, modèle) -> horodatage du dernier échec
_KO_TTL = 3600.0   # on réessaie au bout d'une heure (au cas où ce soit passager)

# ⏱️ DÉLAIS EXPLICITES — indispensables.
# Les SDK OpenAI et Groq attendent 600 s par défaut, avec 2 nouvelles tentatives :
# un seul fournisseur muet immobilisait Nova ~30 min, et comme la chaîne enchaîne
# plusieurs fournisseurs, elle « réfléchissait » sans jamais répondre.
# Ici : on abandonne vite et on passe au fournisseur suivant, qui lui répondra.
# ⚠️ TIMEOUT_LLM doit rester PETIT devant TIMEOUT_CHAINE, sinon un seul fournisseur muet
# consomme tout le budget et les suivants ne sont jamais essayés — exactement ce que la
# chaîne de secours est censée empêcher (constaté : « nvidia : Request timed out. · délai
# global dépassé — fournisseurs suivants non essayés », alors que Groq marchait).
TIMEOUT_LLM = float(os.getenv("LLM_TIMEOUT", "22"))        # réponse complète, PAR fournisseur
TIMEOUT_STREAM = float(os.getenv("LLM_TIMEOUT_STREAM", "60"))  # streaming (plus long, ça coule)
TIMEOUT_LISTE = 8.0                                         # découverte des modèles
TIMEOUT_CHAINE = float(os.getenv("LLM_TIMEOUT_TOTAL", "70"))   # budget de TOUTE la chaîne
MIN_ESSAIS = 3          # nombre de fournisseurs qui doivent pouvoir être essayés

# Budget restant pour l'appel en cours : chaque client s'y adapte, personne ne déborde.
_BUDGET_APPEL = None    # défini plus bas (après l'import de contextvars)


def _plancher() -> float:
    """En dessous, un appel n'a aucune chance d'aboutir — mais ce seuil ne doit jamais
    contredire un TIMEOUT_LLM volontairement plus court (tests, réglage serré)."""
    return min(6.0, TIMEOUT_LLM)


# Délai imposé pour un MESURAGE (diagnostic). ⚠️ Volontairement un dict de module et non
# un contextvar : le diagnostic s'exécute dans des threads (run_in_executor), où un
# contextvar ne suit pas. Il n'est posé que le temps du test, à la demande explicite de
# l'utilisateur — un appel normal qui tomberait pile dedans serait simplement plus patient.
_TIMEOUT_MESURE = {"s": 0.0}


def _timeout(t: float):
    """Délai httpx : connexion courte, lecture bornée par le budget restant."""
    # ⚠️ Le budget ne peut que RACCOURCIR le délai, jamais l'allonger : le diagnostic
    # avait beau demander 45 s, chaque fournisseur restait coupé à TIMEOUT_LLM (22 s).
    # NVIDIA était donc déclaré « n'a pas répondu en 22,4 s » sans qu'on sache s'il
    # aurait répondu à 30 s — c'est-à-dire sans répondre à la seule question posée.
    if _TIMEOUT_MESURE["s"] > 0:
        t = _TIMEOUT_MESURE["s"]
        try:
            import httpx
            return httpx.Timeout(t, connect=min(10.0, t))
        except Exception:
            return t
    reste = _BUDGET_APPEL.get(0.0) if _BUDGET_APPEL is not None else 0.0
    if reste > 0:
        t = min(reste, max(_plancher(), min(t, reste)))
    try:
        import httpx
        return httpx.Timeout(t, connect=min(6.0, t))
    except Exception:
        return t


# ── Disjoncteur par fournisseur ───────────────────────────────────────────────
# Une clé morte (NVIDIA non vérifiée, Gemini sans API activée…) coûtait son délai plein
# à CHAQUE message. On la met de côté quelques minutes après un échec franc.
_FOURNISSEURS_KO = {}
_KO_FOURNISSEUR_TTL = float(os.getenv("LLM_PANNE_TTL", "600"))


def _fournisseur_hs(nom: str) -> bool:
    """Ce fournisseur est-il encore sous sanction ? La durée dépend de la gravité."""
    import time
    v = _FOURNISSEURS_KO.get(nom)
    if not v:
        return False
    quand, genre = v
    ttl = _KO_FOURNISSEUR_TTL if genre == "mort" else _KO_LENT_TTL
    return (time.monotonic() - quand) < ttl


# Un fournisseur LENT n'est pas un fournisseur MORT : le bannir dix minutes pour un
# simple dépassement de délai prive Nova d'un service qui marche (NIM NVIDIA met parfois
# plus de 20 s à démarrer sur les gros modèles). Sanction courte, et il revient vite.
_KO_LENT_TTL = float(os.getenv("LLM_LENT_TTL", "120"))


def _marque_fournisseur_hs(nom: str, err: Exception) -> None:
    """Écarte temporairement un fournisseur défaillant. La durée dépend de la GRAVITÉ."""
    import time
    t = str(err).lower()
    mort = any(k in t for k in ("401", "unauthorized", "invalid api key", "402", "payment",
                                "403", "forbidden", "connection error", "name or service"))
    lent = any(k in t for k in ("timed out", "timeout", "read timeout"))
    if mort or lent:
        genre = "mort" if mort else "lent"
        _FOURNISSEURS_KO[nom] = (time.monotonic(), genre)
        duree = _KO_FOURNISSEUR_TTL if mort else _KO_LENT_TTL
        logger.warning(f"[LLM] {nom} écarté {int(duree)} s "
                       f"({'panne franche' if mort else 'trop lent'}).")


def _lister_modeles(client):
    """Modèles réellement offerts au compte, avec un délai COURT.

    La liste n'est qu'une aide à l'auto-guérison : si le fournisseur traîne, on
    l'abandonne au bout de quelques secondes plutôt que de faire attendre la réponse.
    `with_options` n'existe pas sur tous les SDK → repli sur l'appel simple.
    """
    try:
        c = client.with_options(timeout=TIMEOUT_LISTE)
    except Exception:
        c = client
    return c.models.list().data


def _est_hs(nom: str, modele: str) -> bool:
    """Ce modèle a-t-il échoué récemment ? Évite de retenter un modèle retiré
    (ex. GLM déprécié) à chaque message : la réponse partirait avec un aller-retour perdu."""
    import time
    t = _MODELES_KO.get((nom, modele))
    return bool(t and (time.monotonic() - t) < _KO_TTL)


def _marque_hs(nom: str, modele: str) -> None:
    import time
    _MODELES_KO[(nom, modele)] = time.monotonic()
    if _MODELES_OK.get(nom) == modele:      # ne plus le proposer comme « connu bon »
        _MODELES_OK.pop(nom, None)


def _modeles_exclus() -> set:
    """Modèles à ne jamais utiliser — utile pour anticiper une dépréciation annoncée.
    Variable Render : MODELES_EXCLUS=glm-5.2,autre-modele"""
    import os
    return {m.strip().lower() for m in (os.environ.get("MODELES_EXCLUS") or "").split(",") if m.strip()}


# ── ROUTAGE PAR TÂCHE ─────────────────────────────────────────────────────────
# Trois niveaux : « rapide » (discuter, mise en forme), « equilibre » (par défaut),
# « puissant » (code, analyse longue, raisonnement).
# Modèle choisi par fournisseur ET par niveau. Si un modèle n'existe plus,
# l'auto-guérison de chaque fournisseur prend le relais : le routage n'échoue jamais.
MODELES = {
    "nvidia": {"rapide": "meta/llama-3.1-8b-instruct",
               "equilibre": "meta/llama-3.3-70b-instruct",
               "puissant": "meta/llama-3.1-405b-instruct"},
    "groq": {"rapide": "llama-3.1-8b-instant",
             "equilibre": "llama-3.3-70b-versatile",
             "puissant": "llama-3.3-70b-versatile"},
    "gemini": {"rapide": "gemini-2.0-flash",
               "equilibre": "gemini-2.0-flash",
               "puissant": "gemini-2.5-pro"},
    "mistral": {"rapide": "open-mistral-nemo",
                "equilibre": "mistral-small-latest",
                "puissant": "mistral-medium-latest"},
    "openrouter": {"rapide": "meta-llama/llama-3.2-3b-instruct:free",
                   "equilibre": "meta-llama/llama-3.3-70b-instruct:free",
                   "puissant": "deepseek/deepseek-r1:free"},
    "cerebras": {"rapide": "llama3.1-8b", "equilibre": "llama-3.3-70b", "puissant": "llama-3.3-70b"},
}

# Ordre des fournisseurs SELON le niveau (choix expliqué à l'utilisateur) :
# — rapide    : Groq d'abord, c'est le plus véloce
# — équilibré : NVIDIA d'abord, quota le plus généreux
# — puissant  : NVIDIA puis Gemini, ce sont eux qui ont les gros modèles
# ⚠️ NVIDIA n'est plus en tête : le diagnostic montre qu'il ne répond pas depuis Render.
# Le laisser premier faisait payer son délai plein à chaque message avant de basculer.
# Il reste dans la chaîne — s'il revient, il resservira.
ORDRE = {
    "rapide":    ("groq", "gemini", "mistral", "openrouter", "cerebras", "nvidia"),
    "equilibre": ("groq", "gemini", "mistral", "openrouter", "cerebras", "nvidia"),
    "puissant":  ("gemini", "mistral", "groq", "openrouter", "cerebras", "nvidia"),
}

# Qui a répondu en dernier (par contexte async → sûr même avec plusieurs requêtes)
import contextvars
DERNIER = contextvars.ContextVar("dernier_llm", default="")
_BUDGET_APPEL = contextvars.ContextVar("budget_appel_llm", default=0.0)
# Dernier fournisseur qui a RÉELLEMENT répondu : on le remet en tête, c'est le seul
# dont on ait la preuve qu'il fonctionne maintenant.
_DERNIER_OK = {"nom": ""}

# ⚠️ La chaîne mettait en tête « celui qui a RÉPONDU en dernier ». C'est bien pour
# éviter un fournisseur en panne, mais ça ne dit rien de sa VITESSE : dès qu'un
# fournisseur lent répondait une fois, il gardait la tête indéfiniment et CHAQUE
# message payait son délai — y compris un « salut ». On retient donc combien de
# temps chacun met réellement, et le plus rapide passe devant. Mêmes modèles,
# même qualité : on demande juste d'abord à celui qui répond vite.
from collections import deque as _deque
_LATENCE = {}                       # nom -> dernières durées observées (secondes)


def _note_latence(nom: str, secondes: float) -> None:
    _LATENCE.setdefault(nom, _deque(maxlen=5)).append(max(0.0, secondes))


def rapidite(nom: str) -> float:
    """Temps de réponse habituel de ce fournisseur, ou l'infini s'il est inconnu."""
    d = _LATENCE.get(nom)
    if not d:
        return float("inf")
    v = sorted(d)
    return v[len(v) // 2]           # médiane : un pic isolé ne fausse pas le choix


def executer_et_capturer(fn, *a, **k):
    """Exécute `fn` DANS LE THREAD COURANT et rend (résultat, modèle réellement utilisé).

    ⚠️ DERNIER est une variable de CONTEXTE : renseignée dans un thread de travail, sa
    valeur ne remonte jamais à la coroutine appelante. L'affichage « quel modèle a
    répondu » était donc toujours vide — la fonctionnalité n'a jamais marché.
    On remet la variable à zéro à l'entrée, car les threads du pool sont RÉUTILISÉS et
    garderaient sinon la valeur d'une requête précédente.
    """
    DERNIER.set("")
    resultat = fn(*a, **k)
    return resultat, DERNIER.get("")


def _modele_impose(nom: str) -> str:
    """Modèle explicitement choisi par l'utilisateur (ex. NVIDIA_MODEL sur Render).
    Présent dans l'environnement = volonté explicite → prioritaire sur le routage auto."""
    import os
    return (os.environ.get(f"{nom.upper()}_MODEL") or "").strip()


# L'utilisateur peut EXIGER un fournisseur : « réponds avec l'api nvidia », « utilise groq ».
_NOMS_FOURNISSEURS = {
    "nvidia": ("nvidia", "nim"), "groq": ("groq",), "gemini": ("gemini", "google ai"),
    "openrouter": ("openrouter", "open router"), "cerebras": ("cerebras",),
    "mistral": ("mistral", "le chat"),
    "xai": ("xai", "grok"),
}


def fournisseur_demande(message: str) -> str:
    """Le fournisseur explicitement réclamé dans le message, sinon "".

    « je veux une réponse avec l'api nvidia » doit être honoré : c'est une consigne
    claire, pas une préférence à deviner.
    """
    m = (message or "").lower()
    if not any(v in m for v in ("api", "modèle", "modele", "utilise", "avec ", "via ",
                                "par ", "réponds", "reponds")):
        return ""
    for nom, alias in _NOMS_FOURNISSEURS.items():
        for a in alias:
            if re.search(r"(?<![\w])" + re.escape(a) + r"(?![\w])", m):
                return nom
    return ""


# Fournisseur choisi par l'utilisateur DANS L'INTERFACE, pour toute la conversation.
# ⚠️ Volontairement un simple dict de module, PAS un contextvar : les appels LLM partent
# dans des threads via run_in_executor, où un contextvar ne suit pas (c'est exactement ce
# qui avait empêché l'affichage du modèle pendant des semaines). Nova est mono-utilisateur,
# un réglage global est donc à la fois suffisant et fiable.
PREFERENCE = {"fournisseur": ""}


def choisir_fournisseur(nom: str) -> str:
    """Fixe le fournisseur préféré. "" ou "auto" = choix automatique."""
    nom = (nom or "").strip().lower()
    PREFERENCE["fournisseur"] = nom if nom in _NOMS_FOURNISSEURS else ""
    return PREFERENCE["fournisseur"]


def fournisseur_choisi() -> str:
    return PREFERENCE.get("fournisseur", "")


# ── Deuxième clé, deuxième quota ─────────────────────────────────────────────
# « si il faut je recrée une deuxième clé Groq et Gemini avec des comptes diff » — oui :
# les limites gratuites se comptent PAR COMPTE. Une deuxième clé entre dans la chaîne
# comme un fournisseur de plus, et Nova y bascule quand la première dit 429.
#
# ⚠️ POURQUOI UN THREAD-LOCAL ET PAS UN contextvar. Les appels au modèle partent dans
# un `run_in_executor` : un contextvar ne franchit PAS cette frontière — c'est
# exactement le piège qui avait coûté le canal des confirmations (voir agent/canal.py).
# `_une_passe` appelle le fournisseur DANS le thread courant, donc un thread-local est
# à la fois juste et sûr, y compris quand deux requêtes se croisent.
_CLE_LOCALE = threading.local()


# ── « Ce modèle n'existe plus » : UNE seule définition ───────────────────────
# ⚠️ NVIDIA a répondu : « Error code: 410 - Gone - The model
# 'meta/llama-3.3-70b-instruct' has reached its end of life ». 410 est le code le plus
# EXPLICITE qui existe pour dire « retiré définitivement »… et c'était le seul que
# l'auto-guérison ne reconnaissait pas. Elle attrapait 404, 400, « not_found »,
# « decommission » — pas 410. Résultat : au lieu de passer au modèle suivant, la
# chaîne NVIDIA s'arrêtait net sur son premier candidat. Le fournisseur entier
# disparaissait pour un mot manquant dans une liste.
#
# Et il y avait SIX listes de ce genre dans ce fichier, toutes un peu différentes.
# Une question posée six fois reçoit six réponses : on la pose une fois.
_MOTS_MODELE_MORT = (
    "not_found", "not found", "does not exist", "no endpoints", "unsupported",
    "decommission", "deprecat", "end of life", "end-of-life", "has reached its",
    "retired", "no longer available", "gone", "invalid_request", "not available",
    "410", "404",
)


def modele_mort(err) -> bool:
    """Ce modèle est-il retiré / inconnu ? (donc : passer au suivant, pas abandonner)"""
    t = str(err or "").lower()
    return any(k in t for k in _MOTS_MODELE_MORT)


def _cle(nom: str) -> str:
    """La clé à utiliser pour ce fournisseur, MAINTENANT, dans ce thread."""
    forcee = (getattr(_CLE_LOCALE, "cles", None) or {}).get(nom)
    if forcee:
        return forcee
    return getattr(config, f"{nom.upper()}_API_KEY", "") or ""


class _AvecCle:
    """Le temps d'un appel, ce fournisseur utilise CETTE clé."""

    def __init__(self, nom: str, cle: str):
        self.nom, self.cle = nom, cle

    def __enter__(self):
        self.avant = dict(getattr(_CLE_LOCALE, "cles", None) or {})
        d = dict(self.avant)
        d[self.nom] = self.cle
        _CLE_LOCALE.cles = d

    def __exit__(self, *a):
        _CLE_LOCALE.cles = self.avant
        return False


# Les fournisseurs qui acceptent une deuxième clé. Écrit ici, en un seul endroit :
# une liste éparpillée finit par n'être à jour qu'à moitié.
_SECONDES_CLES = ("groq", "gemini")


def cles_secondaires() -> dict:
    """{fournisseur: 2e clé} — seulement celles qui sont réellement renseignées."""
    out = {}
    for nom in _SECONDES_CLES:
        c = (getattr(config, f"{nom.upper()}_API_KEY_2", "") or "").strip()
        # Une 2e clé identique à la 1re ne double rien : c'est le même compte, donc la
        # même limite. On l'ignore plutôt que de faire croire à un quota doublé.
        if c and c != (getattr(config, f"{nom.upper()}_API_KEY", "") or "").strip():
            out[nom] = c
    return out


def etat_fournisseurs() -> list:
    """Qui est configuré, qui répond, qui est écarté — pour le sélecteur de l'interface."""
    import time as _t
    cles = {
        "nvidia": getattr(config, "NVIDIA_API_KEY", ""), "groq": config.GROQ_API_KEY,
        "gemini": getattr(config, "GEMINI_API_KEY", ""),
        "openrouter": getattr(config, "OPENROUTER_API_KEY", ""),
        "cerebras": config.CEREBRAS_API_KEY, "xai": getattr(config, "XAI_API_KEY", ""),
        "mistral": getattr(config, "MISTRAL_API_KEY", ""),
    }
    jolis = {"nvidia": "NVIDIA", "groq": "Groq", "gemini": "Gemini",
             "openrouter": "OpenRouter", "cerebras": "Cerebras", "xai": "Grok (xAI)",
             "mistral": "Mistral"}
    out = []
    for nom, cle in cles.items():
        # ⚠️ On stocke le DÉBUT de la sanction, pas sa fin : le temps restant se calcule
        # depuis la durée du genre de panne, sinon on annonce des délais négatifs.
        ko = _FOURNISSEURS_KO.get(nom)
        reste = 0
        genre = ""
        if ko:
            quand, genre = ko
            ttl = _KO_FOURNISSEUR_TTL if genre == "mort" else _KO_LENT_TTL
            reste = max(0, round(ttl - (_t.monotonic() - quand)))
        out.append({
            "nom": nom, "joli": jolis.get(nom, nom.capitalize()),
            "configure": bool(cle),
            "etat": ("non configuré" if not cle else
                     "écarté" if reste > 0 else "prêt"),
            "raison": ("" if not cle else
                       "panne franche" if genre == "mort" and reste else
                       "trop lent" if genre == "lent" and reste else ""),
            "reprend_dans_s": reste,
            "modele": _MODELES_OK.get(nom, "") or MODELES.get(nom, {}).get("equilibre", ""),
            "dernier_ok": _DERNIER_OK["nom"] == nom,
            "vitesse_s": (None if rapidite(nom) == float("inf") else round(rapidite(nom), 1)),
            "choisi": PREFERENCE.get("fournisseur") == nom,
        })
    return out


def _providers_disponibles(niveau: str = "equilibre", impose: str = ""):
    """Chaîne de secours : le fournisseur préféré d'abord, puis TOUS les autres configurés.
    Avant, seuls 2 étaient essayés — si Cerebras passait en payant (402) et Groq atteignait sa
    limite, Nova devenait muette alors qu'une autre clé était peut-être disponible."""
    chaine = []
    tous = {
        "nvidia":     (getattr(config, "NVIDIA_API_KEY", ""), _nvidia_chat),
        "groq":       (config.GROQ_API_KEY, _groq_chat),
        "gemini":     (getattr(config, "GEMINI_API_KEY", ""), _gemini_chat),
        "openrouter": (getattr(config, "OPENROUTER_API_KEY", ""), _openrouter_chat),
        "mistral":    (getattr(config, "MISTRAL_API_KEY", ""), _mistral_chat),
        "cerebras":   (config.CEREBRAS_API_KEY, _cerebras_chat),
        "xai":        (getattr(config, "XAI_API_KEY", ""), _xai_chat),
    }

    def add(nom):
        cle_fn = tous.get(nom)
        if not cle_fn or not cle_fn[0] or nom in [c[0] for c in chaine]:
            return
        # Si TU as choisi un modèle précis (variable ..._MODEL définie sur Render),
        # il l'emporte sur le choix automatique — c'est toi qui décides.
        force = _modele_impose(nom)
        modele = force or MODELES.get(nom, {}).get(niveau) or _MODELES_OK.get(nom) or config.LLM_MODEL
        chaine.append((nom, cle_fn[1], modele))

    def add_2(nom):
        """La 2e clé du même fournisseur, ajoutée APRÈS tous les autres.

        ⚠️ Volontairement en fin de chaîne. Un autre fournisseur non saturé vaut mieux
        qu'un deuxième compte chez celui qui vient de dire 429 : quand Groq limite, ce
        n'est pas toujours par compte (une panne, un modèle retiré), et repartir sur le
        même service serait tomber deux fois dans le même trou.
        """
        cle2 = cles_secondaires().get(nom)
        if not cle2 or not tous.get(nom):
            return
        force = _modele_impose(nom)
        modele = force or MODELES.get(nom, {}).get(niveau) or _MODELES_OK.get(nom) or config.LLM_MODEL
        fn = tous[nom][1]

        def appel(messages, m, temperature, niveau=None, _fn=fn, _nom=nom, _c=cle2):
            with _AvecCle(_nom, _c):
                try:
                    return _fn(messages, m, temperature, niveau)
                except TypeError:
                    return _fn(messages, m, temperature)
        chaine.append((f"{nom} (2e clé)", appel, modele))

    # 0) Fournisseur RÉCLAMÉ par l'utilisateur dans son message → il passe avant tout.
    #    À défaut, celui qu'il a choisi dans l'interface : une consigne écrite dans la
    #    phrase reste plus précise qu'un réglage général, elle garde donc la priorité.
    impose = impose or PREFERENCE.get("fournisseur", "")
    if impose and impose in tous and tous[impose][0]:
        add(impose)
    # 1) Fournisseur imposé EXPLICITEMENT (LLM_PREFER=nvidia par ex.) → en tête.
    #    En mode « auto » on ne force rien : l'ordre par tâche décide (Groq pour le rapide,
    #    NVIDIA pour le courant et le lourd).
    prefere = (getattr(config, "LLM_PREFER", "auto") or "auto").lower()
    if prefere != "auto" and prefere in tous:
        add(prefere)
    # 2) Celui qui a RÉPONDU en dernier passe devant : c'est le seul dont on ait la preuve
    #    qu'il marche à cet instant. Sans ça, chaque message repayait le délai plein du
    #    fournisseur en panne placé en tête par le routage théorique.
    else:
        # Le plus RAPIDE parmi ceux qui ont déjà répondu et qui ne sont pas en panne.
        connus = [n for n in tous
                  if tous[n][0] and rapidite(n) < float("inf") and not _fournisseur_hs(n)]
        if connus:
            add(min(connus, key=rapidite))
        # À défaut de mesure, celui qui a répondu en dernier : c'est le seul dont on
        # ait la preuve qu'il marche à cet instant.
        elif _DERNIER_OK["nom"] and not _fournisseur_hs(_DERNIER_OK["nom"]):
            add(_DERNIER_OK["nom"])
    # 3) Puis l'ordre adapté au niveau de la tâche
    for nom in ORDRE.get(niveau, ORDRE["equilibre"]):
        add(nom)
    # 4) Enfin tout le reste (rien ne doit être oublié)
    for nom in tous:
        add(nom)

    # 4bis) Les deuxièmes clés, tout à la fin : un autre fournisseur d'abord.
    for nom in _SECONDES_CLES:
        add_2(nom)
    # 5) Les fournisseurs en panne récente partent en fin de liste plutôt qu'à la poubelle :
    #    si TOUS sont marqués HS, il faut quand même en tenter un.
    vivants = [c for c in chaine if not _fournisseur_hs(c[0])]
    morts = [c for c in chaine if _fournisseur_hs(c[0])]
    return vivants + morts


def _delai_conseille(err: Exception) -> float:
    """Combien de temps attendre, d'après le fournisseur lui-même.

    Groq répond « Please try again in 7.5s » (ou « in 2m3s ») sur un 429 : autant s'en
    servir plutôt que de deviner.
    """
    t = str(err)
    # On rend la valeur EXACTE : plafonner ici masquerait l'information. C'est à
    # l'appelant de décider combien de temps il accepte d'attendre.
    m = re.search(r"try again in\s+(?:(\d+)m)?\s*([\d.]+)s", t, re.I)
    if m:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    m = re.search(r"retry[- ]after[\"'\s:]+([\d.]+)", t, re.I)
    if m:
        return float(m.group(1))
    return 0.0


def _explique(nom: str, err: Exception) -> str:
    """Message clair pour l'utilisateur selon le type de panne."""
    t = str(err).lower()
    # ⚠️ Le test de limite passe EN PREMIER : le message d'un 429 Groq contient un lien
    # vers « …/settings/billing », ce qui le faisait annoncer comme « offre gratuite
    # épuisée (paiement demandé) » — un diagnostic faux qui envoyait chercher une CB
    # alors qu'il suffisait d'attendre quelques secondes.
    if _is_rate_limit(err):
        d = _delai_conseille(err)
        return f"{nom} : limite atteinte" + (f", à réessayer dans {int(d)} s" if d else "")
    if "payment_required" in t or "402" in t or "insufficient" in t:
        return f"{nom} : offre gratuite épuisée (paiement demandé)"
    if "401" in t or "invalid api key" in t or "unauthorized" in t:
        return f"{nom} : clé invalide"
    if modele_mort(t):
        # ⚠️ Cette phrase est celle qu'il LIT dans le diagnostic. « modèle
        # indisponible » se confond avec « clé morte » et l'envoie régénérer une clé
        # qui marche : on dit que c'est le MODÈLE, et que la clé n'y est pour rien.
        return f"{nom} : ce modèle n'existe plus (la clé, elle, est bonne)"
    return f"{nom} : {str(err)[:70]}"


class ToutSature(RuntimeError):
    """Tous les fournisseurs sont à leur limite — panne PASSAGÈRE, pas définitive."""

    def __init__(self, message: str, delai: float = 0.0):
        super().__init__(message)
        self.delai = delai


def chat(messages: list, temperature: float = 0.7, num_ctx: int = 4096,
         niveau: str = "equilibre", patience: int = 0, impose: str = "") -> str:
    """Appel synchrone. Choisit le modèle adapté au niveau de la tâche, puis essaie
    chaque fournisseur jusqu'à ce qu'un réponde (le routage ne peut donc pas faire échouer).

    `patience` : nombre d'attentes supplémentaires autorisées quand TOUS les fournisseurs
    sont momentanément à leur limite. Réservé aux traitements que personne ne regarde
    (synthèse d'un cours…) : attendre 20 s y vaut infiniment mieux qu'un échec, alors que
    dans un chat il faut répondre tout de suite.
    """
    import time as _tps
    for essai in range(max(1, patience + 1)):
        try:
            return _une_passe(messages, temperature, num_ctx, niveau, impose)
        except ToutSature as e:
            if essai >= patience:
                raise
            # Les limites Groq se comptent PAR MINUTE : le délai annoncé (« try again in
            # 5s ») vaut pour l'instant T, pas après une nouvelle salve. On rallonge donc
            # à chaque tour, sinon on retombe aussitôt sur la même limite.
            attente = min(60.0, max(5.0, (e.delai or 15.0) * (essai + 1) + 2.0))
            logger.warning(f"[LLM] tout est saturé — nouvelle tentative dans {int(attente)} s "
                           f"({essai + 1}/{patience})")
            _tps.sleep(attente)
    raise RuntimeError("Aucun modèle disponible pour le moment.")


def _une_passe(messages: list, temperature: float, num_ctx: int, niveau: str,
               impose: str = "") -> str:
    """Un parcours complet de la chaîne de fournisseurs."""
    chaine = _providers_disponibles(niveau, impose)
    if not chaine:
        return _ollama_chat(messages, config.LLM_MODEL, temperature, num_ctx)

    # ⏱️ Plafond global de la chaîne : même avec des délais par fournisseur, essayer 6
    # fournisseurs à la suite pouvait dépasser plusieurs minutes. Passé ce budget, on
    # arrête d'essayer et on remonte une erreur claire — mieux qu'un silence infini.
    import time as _t
    _fin_chaine = _t.monotonic() + TIMEOUT_CHAINE

    soucis, satures, delai = [], 0, 0.0
    for i, (nom, fn, modele) in enumerate(chaine):
        restant = _fin_chaine - _t.monotonic()
        # Le PREMIER fournisseur est toujours tenté : ne rien essayer serait pire que
        # d'essayer une fois de trop.
        if i > 0 and restant < _plancher():
            soucis.append("délai global dépassé — fournisseurs suivants non essayés")
            break
        # ⏱️ Chaque fournisseur ne reçoit qu'une PART du temps restant, calculée pour qu'au
        # moins MIN_ESSAIS d'entre eux puissent être tentés. C'est ce partage qui manquait :
        # le premier prenait tout et les suivants n'avaient jamais leur chance.
        a_venir = max(1, min(len(chaine) - i, MIN_ESSAIS))
        part = min(TIMEOUT_LLM, restant / a_venir)
        _BUDGET_APPEL.set(min(restant, max(_plancher(), part)))
        _depart = _t.monotonic()
        try:
            try:      # certains fournisseurs exploitent le niveau pour choisir le modèle
                out = fn(messages, modele or config.LLM_MODEL, temperature, niveau)
            except TypeError:
                out = fn(messages, modele or config.LLM_MODEL, temperature)
            if out and out.strip():
                _note_latence(nom, _t.monotonic() - _depart)
                if soucis:
                    logger.warning(f"[LLM] bascule sur {nom} après : {' | '.join(soucis)}")
                DERNIER.set(f"{nom} · {_MODELES_OK.get(nom) or modele}")
                # ⚠️ « groq (2e clé) » n'est pas un nom de fournisseur : rangé tel quel,
                # « celui qui a répondu en dernier passe devant » ne retrouvait plus rien
                # et l'optimisation s'éteignait en silence dès que la 2e clé servait.
                _DERNIER_OK["nom"] = nom.split(" (")[0]
                _FOURNISSEURS_KO.pop(nom, None)      # il remarche : on lève la sanction
                return out
            soucis.append(f"{nom} : réponse vide")
        except Exception as e:
            soucis.append(_explique(nom, e))
            _marque_fournisseur_hs(nom, e)
            if _is_rate_limit(e):
                satures += 1
                delai = max(delai, _delai_conseille(e))
            logger.warning(f"[LLM] {nom} indisponible → fournisseur suivant. ({str(e)[:90]})")
        finally:
            _BUDGET_APPEL.set(0.0)

    # Dernier recours : Ollama local (souvent absent en ligne)
    try:
        return _ollama_chat(messages, config.LLM_MODEL, temperature, num_ctx)
    except Exception:
        pass
    # Tout le monde à sa limite = panne PASSAGÈRE : on le signale à part pour que
    # l'appelant puisse simplement attendre au lieu de renoncer.
    if satures and satures >= len([s for s in soucis if "vide" not in s]):
        raise ToutSature(
            "Tous les modèles sont à leur limite pour le moment.\n• " + "\n• ".join(soucis),
            delai)
    raise RuntimeError(
        "Aucun modèle disponible pour le moment.\n• " + "\n• ".join(soucis) +
        "\n\nAjoute ou renouvelle une clé gratuite : console.groq.com (Groq) "
        "ou aistudio.google.com (Gemini), puis mets-la dans les variables Render.")


_BASES = {"nvidia": "https://integrate.api.nvidia.com/v1",
          "groq": "https://api.groq.com/openai/v1",
          "cerebras": "https://api.cerebras.ai/v1",
          "openrouter": "https://openrouter.ai/api/v1"}


def chat_stream(messages: list, temperature: float = 0.6, niveau: str = "equilibre",
                impose: str = ""):
    """Générateur : produit la réponse token par token (vrai streaming).
    Prend le PREMIER fournisseur de la chaîne du niveau qui sait streamer (API OpenAI).
    Gemini/Ollama ne streament pas ici → repli sur une réponse d'un bloc."""
    from openai import OpenAI
    cles = {"nvidia": getattr(config, "NVIDIA_API_KEY", ""), "groq": config.GROQ_API_KEY,
            "cerebras": config.CEREBRAS_API_KEY,
            "openrouter": getattr(config, "OPENROUTER_API_KEY", "")}
    provider, model, client = None, config.LLM_MODEL, None
    for nom, _fn, mod in _providers_disponibles(niveau, impose):
        if nom in _BASES and cles.get(nom):
            provider = nom
            model = _MODELES_OK.get(nom) or mod
            client = OpenAI(api_key=cles[nom], base_url=_BASES[nom],
                            timeout=_timeout(TIMEOUT_STREAM), max_retries=0)
            break
    ecrits = 0          # caractères réellement envoyés à l'écran (voir plus bas)
    try:
        if client is None:
            # Aucun fournisseur « streamable » → réponse complète d'un coup
            yield chat(messages, temperature=temperature, niveau=niveau, impose=impose); return
        DERNIER.set(f"{provider} · {model}")
        total = 0
        stream = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=4096, stream=True)
        for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content
            except Exception:
                delta = None
            if delta:
                total += 1
                ecrits += len(delta.strip())
                yield delta
        try:
            from llm.usage import record
            record(total, provider=provider)  # approx (tokens ≈ chunks)
        except Exception:
            pass
        # ⚠️ Un flux qui se termine sans avoir rien écrit n'était pas une erreur : la
        # boucle ne produisait rien, aucune exception n'était levée, la fonction
        # rendait la main normalement. Nova affichait une bulle TOTALEMENT VIDE et se
        # déclarait terminée. Cas courant chez Groq quand le modèle part sur un
        # tool-call vide, ou quand le contenu est filtré. Le chemin non streamé se
        # protège pourtant de ce cas depuis toujours (`if out and out.strip()`).
        if ecrits == 0:
            logger.warning(f"[chat_stream] {provider} n'a rien écrit → repli non-stream")
            secours = chat(messages, temperature=temperature, niveau=niveau, impose=impose)
            if secours and secours.strip():
                yield secours
            else:
                yield ("⚠️ Le modèle n'a rien renvoyé cette fois — c'est un raté de son "
                       "côté, pas de ta question. Redemande-moi.")
    except Exception as e:
        logger.warning(f"[chat_stream] échec streaming ({str(e)[:80]}) → repli non-stream")
        # ⚠️ Ce repli ne savait pas que du texte était DÉJÀ parti à l'écran. Quand la
        # connexion cassait en cours de route, il collait une réponse entièrement
        # neuve derrière une phrase coupée au milieu : Lohan lisait « Le théorème de »
        # suivi d'une seconde réponse repartant du début, souvent contradictoire — et
        # c'est cette bouillie qui était mémorisée. On ne relance une réponse complète
        # que si rien n'a encore été affiché.
        if ecrits:
            yield ("\n\n⚠️ _Ma réponse a été coupée en cours de route (connexion au "
                   "modèle interrompue). Redemande-moi pour l'avoir en entier._")
            return
        try:
            yield chat(messages, temperature=temperature, niveau=niveau, impose=impose)
        except Exception as e2:
            yield f"❌ Erreur LLM : {str(e2)[:200]}"


def _groq_chat(messages: list, model: str, temperature: float) -> str:
    """Groq avec auto-guérison : les modèles sont régulièrement retirés (404).
    On essaie le modèle configuré, des noms connus, puis ceux réellement offerts au compte."""
    from groq import Groq
    client = Groq(api_key=_cle("groq"), timeout=_timeout(TIMEOUT_LLM), max_retries=0)

    candidats = []
    for m in (_modele_memorise("groq"), model, config.GROQ_MODEL,
              "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
              "meta-llama/llama-4-scout-17b-16e-instruct",
              "openai/gpt-oss-120b", "qwen/qwen3-32b", "gemma2-9b-it"):
        if m and m not in candidats:
            candidats.append(m)
    try:                                   # modèles réellement disponibles sur CE compte
        decouverts = []
        for mo in _lister_modeles(client):
            mid = getattr(mo, "id", "") or ""
            bas = mid.lower()
            # On exclut ce qui n'est pas un modèle de chat GÉNÉRALISTE. « compound » =
            # systèmes agentiques (ils refusent nos paramètres) ; « allam » et consorts
            # sont spécialisés dans une autre langue — ils répondent sans erreur, mais
            # mal, et c'était donc invisible.
            if mid and mid not in candidats and not any(k in bas for k in _MODELES_INADAPTES):
                decouverts.append(mid)
        # Les familles connues d'abord : un modèle inconnu n'est essayé qu'en dernier.
        candidats.extend(sorted(decouverts, key=_rang_modele))
    except Exception:
        pass

    derniere, satures = None, 0
    for m in [c for c in candidats if not _est_hs('groq', c)] or candidats:
        try:
            resp = client.chat.completions.create(
                model=m, messages=messages, temperature=temperature, max_tokens=4096)
            _retenir_modele("groq", m)
            try:
                from llm.usage import record
                record(getattr(getattr(resp, "usage", None), "total_tokens", 0))
            except Exception:
                pass
            return resp.choices[0].message.content or ""
        except Exception as e:
            derniere = e
            t = str(e).lower()
            # Modèle retiré (404) OU incompatible avec un chat simple (400) → on tente le suivant.
            # Sans le cas 400, un seul modèle capricieux faisait échouer tout Groq.
            if modele_mort(t) or any(k in t for k in (
                                    "no longer", "400", "tool choice", "unsupported",
                                    "invalid_request", "does not support")):
                _marque_hs("groq", m)
                logger.warning(f"[LLM] Groq : modèle '{m}' inutilisable ({str(e)[:60]}), essai suivant…")
                continue
            # Chez Groq le quota est compté PAR MODÈLE : le 70B saturé ne dit RIEN du 8B,
            # qui dispose de son propre budget. On ne quittait pourtant Groq entièrement,
            # alors qu'un modèle plus léger aurait répondu tout de suite.
            if _is_rate_limit(e) and satures < 4:
                satures += 1
                logger.warning(f"[LLM] Groq : '{m}' à sa limite, essai d'un modèle plus léger…")
                continue
            raise
    raise RuntimeError(f"Groq : aucun modèle texte accessible ({derniere})")


def _score_niveau(mid: str, niveau: str) -> int:
    """Note un modèle selon le niveau voulu, d'après son NOM.
    Permet d'exploiter tout le catalogue réel du compte (GLM, Nemotron, DeepSeek…)
    au lieu d'une liste figée qui devient périmée."""
    b = mid.lower()
    petit = any(k in b for k in ("lightning", "mini", "small", "nano", "flash", "instant",
                                 "-xs", "-1b", "-2b", "-3b", "-4b", "-7b", "-8b", "-9b"))
    gros = any(k in b for k in ("ultra", "super", "-pro", "max", "405b", "550b", "480b",
                                "253b", "235b", "120b", "-r1", "reasoning", "thinking"))
    # Catégories EXCLUSIVES : sinon « super-49b » cumulait les points « gros » et « moyen »
    # et passait devant un vrai grand modèle comme « ultra-550b ».
    if gros:
        petit = moyen = False
    elif petit:
        moyen = False
    else:
        # Sans indice de taille (ex. « glm-5.2 »), on suppose un modèle polyvalent
        moyen = True
    # Familles reconnues pour leur polyvalence et l'usage d'outils (essentiel pour un agent)
    bonus = 3 if any(k in b for k in ("glm", "nemotron", "deepseek", "gpt-oss")) else \
        (2 if any(k in b for k in ("llama", "qwen", "mistral", "gemma", "minimax", "step")) else 0)
    if niveau == "rapide":
        return (6 if petit else 0) + (2 if moyen else 0) - (4 if gros else 0) + bonus
    if niveau == "puissant":
        return (6 if gros else 0) + (2 if moyen else 0) - (4 if petit else 0) + bonus
    return (5 if moyen else 0) + (1 if petit else 0) + bonus


def _nvidia_chat(messages: list, model: str, temperature: float, niveau: str = "equilibre") -> str:
    """NVIDIA NIM (build.nvidia.com) — API compatible OpenAI, offre gratuite généreuse.
    La clé donne accès à TOUT le catalogue : on liste les modèles réellement disponibles
    et on prend le mieux adapté au niveau de la tâche."""
    from openai import OpenAI
    if not config.NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY absente.")
    client = OpenAI(api_key=config.NVIDIA_API_KEY,
                    base_url="https://integrate.api.nvidia.com/v1",
                    timeout=_timeout(TIMEOUT_LLM), max_retries=0)

    candidats = []
    impose = _modele_impose("nvidia")
    for m in (impose, _MODELES_OK.get("nvidia") if not impose else None, model if not impose else None):
        if m and m not in candidats:
            candidats.append(m)
    # Catalogue réel du compte, classé par adéquation au niveau
    if not impose:
        try:
            dispo = []
            for mo in _lister_modeles(client):
                mid = getattr(mo, "id", "") or ""
                b = mid.lower()
                # Le catalogue mêle des modèles qui ne savent pas discuter (vidéo, embeddings,
                # biologie, détection, traduction, sécurité) : on ne garde que les conversationnels.
                if mid and not any(k in b for k in (
                        "embed", "rerank", "ocr", "speech", "tts", "riva", "guard", "diffusion",
                        "video", "vila", "clip", "parakeet", "cosmos", "ising", "bevformer",
                        "esm", "fold", "voicechat", "content-safety", "speaker", "noise",
                        "detector", "translate", "calibration", "retriever", "protein")):
                    dispo.append(mid)
            exclus = _modeles_exclus()
            dispo = [m for m in dispo
                     if not _est_hs("nvidia", m)
                     and not any(x in m.lower() for x in exclus)]
            dispo.sort(key=lambda x: -_score_niveau(x, niveau))
            for mid in dispo:
                if mid not in candidats:
                    candidats.append(mid)
        except Exception:
            pass
        for m in ("meta/llama-3.3-70b-instruct", "meta/llama-3.1-8b-instruct"):   # ultime secours
            if m not in candidats:
                candidats.append(m)

    derniere = None
    for m in candidats:
        try:
            resp = client.chat.completions.create(
                model=m, messages=messages, temperature=temperature, max_tokens=4096)
            _retenir_modele("nvidia", m)
            try:
                from llm.usage import record
                record(getattr(getattr(resp, "usage", None), "total_tokens", 0), provider="nvidia")
            except Exception:
                pass
            return resp.choices[0].message.content or ""
        except Exception as e:
            derniere = e
            t = str(e).lower()
            if modele_mort(t):
                _marque_hs("nvidia", m)      # retiré/déprécié → on ne le retente pas de suite
                logger.warning(f"[LLM] NVIDIA : modèle '{m}' inutilisable, essai suivant…")
                continue
            raise
    raise RuntimeError(f"NVIDIA : aucun modèle accessible ({derniere})")


def _openrouter_chat(messages: list, model: str, temperature: float) -> str:
    """OpenRouter — une clé, des dizaines de modèles (dont beaucoup de gratuits, suffixe « :free »).
    API compatible OpenAI. Sert de filet universel quand les autres fournisseurs tombent."""
    from openai import OpenAI
    if not getattr(config, "OPENROUTER_API_KEY", ""):
        raise RuntimeError("OPENROUTER_API_KEY absente.")
    client = OpenAI(api_key=config.OPENROUTER_API_KEY,
                    base_url="https://openrouter.ai/api/v1",
                    timeout=_timeout(TIMEOUT_LLM), max_retries=0)

    candidats = []
    for m in (_MODELES_OK.get("openrouter"), model, config.OPENROUTER_MODEL,
              "meta-llama/llama-3.3-70b-instruct:free",
              "deepseek/deepseek-chat:free",
              "qwen/qwen-2.5-72b-instruct:free",
              "google/gemma-2-9b-it:free",
              "meta-llama/llama-3.2-3b-instruct:free"):
        if m and m not in candidats:
            candidats.append(m)

    derniere = None
    for m in candidats:
        try:
            resp = client.chat.completions.create(
                model=m, messages=messages, temperature=temperature, max_tokens=4096,
                extra_headers={"X-Title": "Nova"})
            _retenir_modele("openrouter", m)
            try:
                from llm.usage import record
                record(getattr(getattr(resp, "usage", None), "total_tokens", 0), provider="openrouter")
            except Exception:
                pass
            return resp.choices[0].message.content or ""
        except Exception as e:
            derniere = e
            t = str(e).lower()
            if modele_mort(t) or any(k in t for k in (
                                    "unsupported", "invalid_request", "not available")):
                _marque_hs("openrouter", m)
                logger.warning(f"[LLM] OpenRouter : modèle '{m}' inutilisable, essai suivant…")
                continue
            raise
    raise RuntimeError(f"OpenRouter : aucun modèle accessible ({derniere})")


def _xai_chat(messages: list, model: str, temperature: float) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=config.XAI_API_KEY, base_url="https://api.x.ai/v1",
                    timeout=_timeout(TIMEOUT_LLM), max_retries=0)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=4096,
    )
    return resp.choices[0].message.content or ""


def chat_vision(image_path: str, prompt: str = "", temperature: float = 0.4) -> str:
    """
    Analyse une image. Essaie Groq (Llama 4 Scout) puis Gemini en repli :
    l'analyse de photos ne dépend donc plus d'un seul fournisseur.
    """
    import base64
    import mimetypes

    with open(image_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    question = prompt or "Décris cette image en détail, en français."
    errors = []
    essayes = []          # ce qu'on a REELLEMENT tenté, pour que le détail serve à qqch

    # 1) Groq vision — auto-guérison : les noms de modèles changent souvent (404).
    #    On essaie le modèle configuré, des noms connus, puis ceux réellement accessibles au compte.
    if config.GROQ_API_KEY:
        candidates = []
        for m in (config.GROQ_VISION_MODEL,
                  # ⚠️ Groq a retiré llama-4-scout ET llama-4-maverick le 17 juin 2026,
                  # et les llama-3.2-vision avant eux : les QUATRE noms de la liste
                  # précédente rendaient 404. Ceux-ci sont les remplaçants annoncés.
                  "qwen/qwen3.6-27b",
                  "meta-llama/llama-4-scout-17b-16e-instruct",
                  "meta-llama/llama-4-maverick-17b-128e-instruct"):
            if m and m not in candidates:
                candidates.append(m)
        try:
            from groq import Groq
            client = Groq(api_key=_cle("groq"), timeout=_timeout(TIMEOUT_LLM), max_retries=0)
            # ⚠️ L'AUTO-GUÉRISON EXISTAIT ET NE GUÉRISSAIT PLUS RIEN. Elle ne retenait
            # de la liste du compte que les noms contenant « vision », « scout »,
            # « maverick » ou « llava » — la convention de nommage de 2025. Le modèle
            # multimodal actuel s'appelle « qwen/qwen3.6-27b » : aucun de ces mots.
            # Le filtre encodait une MODE de nommage, pas une capacité.
            # On ne devine donc plus d'après le nom : on essaie ce que le compte offre
            # vraiment. Un modèle qui ne lit pas les images répond une erreur nette, et
            # elle est notée — mieux vaut deux essais de trop qu'un « aucun modèle de
            # vision » alors qu'il y en a un.
            try:
                indices, autres = [], []
                for mo in _lister_modeles(client):
                    mid = getattr(mo, "id", "") or ""
                    bas = mid.lower()
                    if not mid or mid in candidates:
                        continue
                    # Ceux-là ne liront jamais une image : inutile de les déranger.
                    if any(k in bas for k in ("whisper", "tts", "guard", "embed",
                                              "moderation", "compound")):
                        continue
                    if any(k in bas for k in ("vision", "scout", "maverick", "llava",
                                              "vl", "multimodal", "omni")):
                        indices.append(mid)
                    else:
                        autres.append(mid)
                # Les noms qui SUGGÈRENT la vision d'abord, puis le reste du compte —
                # borné, pour ne pas transformer une analyse d'image en attente sans fin.
                candidates.extend(indices + autres[:6])
            except Exception:
                pass
            for m in candidates:
                try:
                    resp = client.chat.completions.create(
                        model=m,
                        messages=[{"role": "user", "content": [
                            {"type": "text", "text": question},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ]}],
                        temperature=temperature, max_tokens=2048,
                    )
                    out = resp.choices[0].message.content or ""
                    if out.strip():
                        return out
                except Exception as e:
                    txt = str(e).lower()
                    if modele_mort(txt):
                        # ⚠️ Un 404 partait en silence : le detail final disait « aucun
                        # modèle de vision sur ce compte » sans dire LESQUELS avaient été
                        # essayés. Impossible de savoir s'il fallait mettre la liste à
                        # jour ou chercher ailleurs — c'est-à-dire un diagnostic qui ne
                        # diagnostique rien.
                        logger.warning(f"[vision] modèle Groq '{m}' indisponible, essai suivant…")
                        essayes.append(f"Groq/{m}=404")
                        continue
                    # ⚠️ On faisait « break » ici : la PREMIÈRE erreur autre qu'un 404 —
                    # une limite de débit sur le modèle le plus demandé, par exemple —
                    # abandonnait toute la liste, y compris des modèles qui auraient
                    # répondu. Un quota atteint sur un modèle ne dit rien des autres.
                    logger.warning(f"[vision] Groq '{m}' a échoué ({str(e)[:70]}), essai suivant…")
                    errors.append(f"Groq({m}): {str(e)[:90]}")
                    continue
            if not any(x.startswith("Groq(") for x in errors):
                errors.append("Groq: aucun modèle de vision sur ce compte")
        except Exception as e:
            errors.append(f"Groq: {str(e)[:120]}")

    # 2) Gemini vision (gratuit, sans carte bancaire)
    # ⚠️ MÊME DÉFAUT QUE CÔTÉ GROQ, ET JE VENAIS DE LE CORRIGER LÀ-BAS : un SEUL nom de
    # modèle, aucun repli. Si celui de la configuration a été renommé, retiré, ou n'est
    # pas disponible sur ce compte, Gemini échoue en 404 et le repli entier tombe —
    # alors que d'autres modèles du même fournisseur savent lire une image.
    # Corriger un défaut d'un côté sans regarder le côté symétrique, c'est le laisser
    # en place là où il fera exactement le même mal.
    if getattr(config, "GEMINI_API_KEY", ""):
        candidats = []
        for m in (getattr(config, "GEMINI_MODEL", ""), "gemini-2.5-flash",
                  "gemini-2.0-flash", "gemini-2.5-pro", "gemini-1.5-flash"):
            if m and m not in candidats:
                candidats.append(m)
        corps = {
            "contents": [{"parts": [{"text": question},
                                    {"inline_data": {"mime_type": mime, "data": b64}}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 2048},
        }
        for m in candidats:
            try:
                import requests
                url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                       f"{m}:generateContent")
                r = requests.post(url, headers=_gemini_auth(), json=corps, timeout=90)
                if r.status_code in (400, 401, 403):   # repli : ancienne méthode ?key=
                    r = requests.post(url, params={"key": config.GEMINI_API_KEY},
                                      json=corps, timeout=90)
                if r.status_code == 200:
                    parts = ((r.json().get("candidates") or [{}])[0]
                             .get("content", {}).get("parts", []))
                    out = "".join(p.get("text", "") for p in parts).strip()
                    if out:
                        return out
                if r.status_code == 404:
                    logger.warning(f"[vision] modèle Gemini '{m}' inconnu, essai suivant…")
                    essayes.append(f"Gemini/{m}=404")
                    continue
                errors.append(f"Gemini({m}): HTTP {r.status_code}")
            except Exception as e:
                errors.append(f"Gemini({m}): {str(e)[:90]}")
        if not any(x.startswith("Gemini(") for x in errors):
            # ⚠️ « aucun modèle de vision sur ce compte » était une CONCLUSION, pas une
            # observation. Quand les quatre noms rendent 404 — y compris gemini-2.5-flash,
            # qui existe bel et bien — ce n'est pas la liste qui est périmée : c'est la
            # clé qui n'a pas accès à l'API Generative Language (projet sans l'API
            # activée). Google répond 404 sur le chemin du modèle dans ce cas. Dire
            # « ce compte n'a pas de modèle de vision » envoie chercher au mauvais endroit.
            tous_404 = sum(1 for e in essayes if e.startswith("Gemini/")) >= 3
            errors.append(
                "Gemini: les 4 modèles répondent 404 — ce n'est probablement pas la "
                "liste qui est périmée, mais la clé qui n'a pas accès à l'API "
                "Generative Language (à activer sur aistudio.google.com)"
                if tous_404 else "Gemini: aucun modèle de vision sur ce compte")

    # 3) OpenRouter — ⚠️ LA PISTE QU'IL AVAIT DÉJÀ SOUS LA MAIN. OpenRouter est déjà
    # un fournisseur de la chaîne de modèles (voir MODELES/ORDRE plus haut) : la clé
    # existe donc, et il propose des modèles de vision en accès GRATUIT. Chercher une
    # API obscure alors qu'un fournisseur déjà branché fait le travail, c'est ajouter
    # une dépendance de plus à surveiller pour rien.
    if getattr(config, "OPENROUTER_API_KEY", ""):
        for m in ("meta-llama/llama-3.2-11b-vision-instruct:free",
                  "qwen/qwen2.5-vl-72b-instruct:free",
                  "google/gemma-3-27b-it:free",
                  "mistralai/mistral-small-3.2-24b-instruct:free"):
            try:
                import requests
                r = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                             "Content-Type": "application/json"},
                    json={"model": m, "temperature": temperature, "max_tokens": 2048,
                          "messages": [{"role": "user", "content": [
                              {"type": "text", "text": question},
                              {"type": "image_url",
                               "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]},
                    timeout=90)
                if r.status_code == 200:
                    out = ((r.json().get("choices") or [{}])[0]
                           .get("message", {}).get("content") or "").strip()
                    if out:
                        return out
                if r.status_code in (404, 400):
                    logger.warning(f"[vision] modèle OpenRouter '{m}' indisponible, suivant…")
                    essayes.append(f"OpenRouter/{m}={r.status_code}")
                    continue
                errors.append(f"OpenRouter({m}): HTTP {r.status_code}")
            except Exception as e:
                errors.append(f"OpenRouter({m}): {str(e)[:90]}")
        if not any(x.startswith("OpenRouter(") for x in errors):
            errors.append("OpenRouter: aucun modèle de vision accessible")

    # ⚠️ CE MESSAGE DISAIT D'AJOUTER UNE CLÉ QU'IL AVAIT DÉJÀ. « Ajoute GROQ_API_KEY »
    # s'affichait alors que sa clé Groq marchait parfaitement pour tout le reste — il
    # serait donc parti la reconfigurer pour rien. Un diagnostic faux coûte plus cher
    # qu'une erreur franche : il envoie chercher au mauvais endroit.
    # On dit maintenant ce qui manque VRAIMENT, en regardant ce qu'on a.
    a_groq = bool(config.GROQ_API_KEY)
    a_gemini = bool(getattr(config, "GEMINI_API_KEY", ""))
    a_or = bool(getattr(config, "OPENROUTER_API_KEY", ""))
    detail = (f" Détail technique : {' | '.join(errors)}" if errors else "")
    if essayes:
        detail += f" · Modèles essayés : {', '.join(essayes[:10])}"
    if not a_groq and not a_gemini and not a_or:
        raise RuntimeError(
            "Je ne peux pas regarder d'image : aucune clé de vision n'est configurée. "
            "Ajoute GROQ_API_KEY (gratuit sur console.groq.com) ou GEMINI_API_KEY "
            "(gratuit sur aistudio.google.com) dans les variables Render." + detail)
    satures = [e for e in errors if any(k in e.lower() for k in
                                        ("rate", "429", "quota", "limit", "capacity"))]
    if satures:
        raise RuntimeError(
            "Je ne peux pas regarder cette image maintenant : les modèles de vision "
            "gratuits sont saturés. Ta configuration est bonne — c'est une limite de "
            "débit, ça repart généralement en quelques minutes. Redemande-moi tout à "
            "l'heure." + detail)
    fournisseurs = " et ".join(x for x in ("Groq" if a_groq else "",
                                           "Gemini" if a_gemini else "",
                                           "OpenRouter" if a_or else "") if x)
    # ⚠️ « Envoie-moi le détail et je mettrai la liste à jour » : c'est ce que disait ce
    # message, et c'est une impasse — il attend une mise à jour, moi j'attends sa
    # capture, et pendant ce temps il ne peut pas lire ses images. Il y a une action
    # qu'il peut faire LUI, tout de suite, gratuitement et sans carte bancaire :
    # OpenRouter donne accès à plusieurs modèles de vision libres. Quand cette clé
    # manque, c'est ELLE qu'il faut nommer, pas une liste que je corrigerai un jour.
    suite = (
        "\n\n**Ce que tu peux faire maintenant :** crée une clé gratuite sur "
        "**openrouter.ai** (aucune carte bancaire) et mets-la dans les variables Render "
        "sous le nom `OPENROUTER_API_KEY`. Elle donne accès à plusieurs modèles capables "
        "de lire une image, et elle servira aussi de secours quand Groq est saturé."
        if not a_or else
        "\n\nEnvoie-moi le détail ci-dessous : il nomme chaque modèle essayé avec son "
        "code de retour, et je saurai lequel remplacer.")
    raise RuntimeError(
        f"Je ne peux pas regarder d'image : ta clé {fournisseurs} fonctionne pour le "
        "texte, mais aucun modèle capable de LIRE une image n'est accessible avec elle. "
        "Ce n'est donc pas une clé à refaire." + suite + detail)


def _mistral_chat(messages: list, model: str, temperature: float) -> str:
    """Mistral — API compatible OpenAI, offre gratuite sans carte bancaire.

    Ajouté parce que NVIDIA ne répond plus depuis Render : mieux vaut une chaîne large
    qu'un fournisseur de moins. Bonus : modèles entraînés en français, ce qui se voit
    sur la qualité des réponses de Nova.
    """
    from openai import OpenAI

    if not getattr(config, "MISTRAL_API_KEY", ""):
        raise RuntimeError("MISTRAL_API_KEY absente — impossible d'appeler Mistral.")
    client = OpenAI(api_key=config.MISTRAL_API_KEY,
                    base_url="https://api.mistral.ai/v1",
                    timeout=_timeout(TIMEOUT_LLM), max_retries=0)
    candidats = [m for m in (model, _MODELES_OK.get("mistral"),
                             "mistral-small-latest", "open-mistral-nemo",
                             "mistral-medium-latest") if m]
    derniere = None
    for m in dict.fromkeys(candidats):
        try:
            r = client.chat.completions.create(
                model=m, messages=messages, temperature=temperature)
            _retenir_modele("mistral", m)
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            derniere = e
            # Modèle inconnu ou retiré : on essaie le suivant plutôt que d'abandonner.
            if not any(k in str(e).lower() for k in ("404", "not found", "model")):
                raise
    raise derniere or RuntimeError("Mistral : aucun modèle disponible")


def _cerebras_chat(messages: list, model: str, temperature: float) -> str:
    """
    Cerebras Inference — API compatible OpenAI (même format que Groq).
    Auto-guérison : si le modèle demandé n'existe pas (404), on réessaie avec
    des modèles Cerebras connus (le 8B est toujours dispo en gratuit).
    """
    from openai import OpenAI

    if not config.CEREBRAS_API_KEY:
        raise RuntimeError("CEREBRAS_API_KEY absente — impossible d'appeler Cerebras.")

    client = OpenAI(
        api_key=config.CEREBRAS_API_KEY,
        base_url="https://api.cerebras.ai/v1",
        timeout=_timeout(TIMEOUT_LLM), max_retries=0,
    )
    # Ordre d'essai : modèle configuré, noms connus, PUIS les modèles réellement
    # accessibles au compte (via /models) → évite les 404 'model_not_found'.
    candidates = []
    for m in (model, "llama-3.3-70b", "llama3.1-8b", "llama-3.1-8b",
              "llama3.1-70b", "llama-4-scout-17b-16e-instruct", "qwen-3-32b"):
        if m and m not in candidates:
            candidates.append(m)
    try:
        for mo in _lister_modeles(client):
            mid = getattr(mo, "id", None)
            if mid and mid not in candidates:
                candidates.append(mid)
    except Exception:
        pass

    last_err = None
    for m in candidates:
        try:
            resp = client.chat.completions.create(
                model=m,
                messages=messages,
                temperature=temperature,
                max_tokens=4096,
            )
            try:
                from llm.usage import record
                record(getattr(getattr(resp, "usage", None), "total_tokens", 0), provider="cerebras")
            except Exception:
                pass
            return resp.choices[0].message.content or ""
        except Exception as e:
            last_err = e
            txt = str(e).lower()
            # Modèle inexistant / non accessible → on tente le suivant
            if modele_mort(txt):
                logger.warning(f"[LLM] Cerebras: modèle '{m}' indisponible, essai suivant…")
                continue
            raise
    raise RuntimeError(f"Cerebras: aucun modèle accessible ({last_err})")



def _gemini_auth() -> dict:
    """En-têtes d'authentification Gemini.

    Google émet deux formats de clés : anciennes « AIza… » (paramètre ?key=) et nouvelles
    « AQ.… ». L'en-tête x-goog-api-key fonctionne pour LES DEUX — on l'utilise donc en
    priorité, et on garde ?key= en secours pour les intégrations plus anciennes.
    """
    return {"x-goog-api-key": (_cle("gemini") or "").strip(),
            "Content-Type": "application/json"}


def _gemini_chat(messages: list, model: str, temperature: float) -> str:
    """
    Google AI Studio (Gemini) via l'API REST generativeLanguage.
    Pas de dépendance lourde : utilise requests (déjà présent).

    Convertit le format OpenAI (role/content) vers le format Gemini :
      - role 'system'    → systemInstruction
      - role 'assistant' → 'model'
      - role 'user'      → 'user'
    """
    import requests
    import time as _t

    if not _cle("gemini"):
        raise RuntimeError("GEMINI_API_KEY absente — impossible d'appeler Gemini.")

    # ⚠️ Gemini etait le SEUL fournisseur reste hors du systeme de delais : requests
    # recevait 20 s puis 120 s CODES EN DUR, qui ignorent _BUDGET_APPEL et TIMEOUT_LLM.
    # Quand Google etait lent ou muet, un seul appel mangeait 1,7x le budget de toute
    # la chaine : les fournisseurs suivants n'etaient jamais essayes, et Lohan lisait
    # apres 90 s « Aucun modele disponible — renouvelle une cle gratuite » alors que
    # Groq et Mistral repondaient parfaitement. Un diagnostic FAUX qui l'envoyait
    # renouveler des cles saines. C'est exactement la panne que l'en-tete de ce
    # fichier dit avoir corrigee pour NVIDIA.
    _budget = _timeout(TIMEOUT_LLM)
    _lim = float(getattr(_budget, "read", None) or getattr(_budget, "timeout", None)
                 or (_budget if isinstance(_budget, (int, float)) else TIMEOUT_LLM))
    _lim = max(5.0, min(_lim, TIMEOUT_LLM))
    _echeance = _t.monotonic() + _lim

    system_parts: list[str] = []
    contents: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "") or ""
        if role == "system":
            system_parts.append(text)
            continue
        g_role = "model" if role == "assistant" else "user"
        contents.append({"role": g_role, "parts": [{"text": text}]})

    payload: dict = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 4096,
        },
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

    # Auto-guérison : les noms de modèles Gemini changent (404). On essaie le modèle
    # configuré, des noms connus, puis ceux réellement offerts par l'API.
    candidats = []
    for m in (_MODELES_OK.get("gemini"), model, config.GEMINI_MODEL,
              "gemini-2.0-flash", "gemini-flash-latest", "gemini-2.5-flash",
              "gemini-1.5-flash", "gemini-2.5-pro"):
        if m and m not in candidats:
            candidats.append(m)
    try:
        # Le catalogue est un CONFORT : il ne doit jamais manger le budget de la reponse.
        lst = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                           headers=_gemini_auth(), timeout=min(8.0, _lim / 3))
        if lst.status_code == 200:
            for mo in (lst.json().get("models") or []):
                nom = (mo.get("name") or "").replace("models/", "")
                if nom and nom not in candidats and "generateContent" in (mo.get("supportedGenerationMethods") or []):
                    candidats.append(nom)
    except Exception:
        pass

    resp, derniere = None, ""
    for m in candidats:
        # La BOUCLE aussi est bornee : essayer huit modeles a 120 s chacun revenait au
        # meme probleme, une fois par candidat.
        restant = _echeance - _t.monotonic()
        if restant <= 2.0:
            derniere = derniere or f"budget epuise apres {len(candidats)} candidat(s)"
            break
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        r = requests.post(url, headers=_gemini_auth(), json=payload, timeout=restant)
        if r.status_code in (400, 401, 403):          # repli : ancienne méthode ?key=
            restant = max(2.0, _echeance - _t.monotonic())
            r = requests.post(url, params={"key": _cle("gemini")}, json=payload,
                              timeout=restant)
        if r.status_code == 200:
            _retenir_modele("gemini", m)
            resp = r
            break
        derniere = f"{r.status_code}: {r.text[:200]}"
        if r.status_code in (404, 400):
            _marque_hs("gemini", m)
            logger.warning(f"[LLM] Gemini : modèle '{m}' indisponible, essai suivant…")
            continue
        break

    if resp is None or resp.status_code != 200:
        # Un 403 avec une clé valide signifie presque toujours que l'API n'est pas activée
        # sur le projet Google (ou que la clé est restreinte) : on le dit clairement.
        d = str(derniere)
        if d.startswith("403"):
            cause = "clé restreinte" if "restrict" in d.lower() or "referer" in d.lower() else \
                    "API « Generative Language » non activée sur le projet Google"
            raise RuntimeError(
                f"Gemini refusé (403) — {cause}.\n"
                "→ Va sur aistudio.google.com, crée la clé depuis « Get API key » en choisissant "
                "un projet, et vérifie que la clé n'a AUCUNE restriction d'application/site.\n"
                f"Détail : {d[:220]}")
        raise RuntimeError(f"Gemini HTTP {d[:260] or 'aucun modèle accessible'}")

    data = resp.json()
    # Consommation Gemini : sans cela, la jauge d'énergie l'ignorait complètement
    try:
        from llm.usage import record
        record(int((data.get("usageMetadata") or {}).get("totalTokenCount", 0) or 0),
               provider="gemini")
    except Exception:
        pass

    candidates = data.get("candidates") or []
    if not candidates:
        # Réponse vide possible si bloquée par les filtres de sécurité
        feedback = data.get("promptFeedback", {})
        raise RuntimeError(f"Gemini: réponse vide (feedback={feedback}).")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Gemini: aucun texte dans la réponse.")
    return text


def _ollama_chat(messages: list, model: str, temperature: float, num_ctx: int) -> str:
    import ollama
    client = ollama.Client(timeout=120)
    resp = client.chat(
        model=model,
        messages=messages,
        options={"temperature": temperature, "num_ctx": num_ctx},
    )
    return resp["message"]["content"]
