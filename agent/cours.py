"""
Mode Cours — Nova écoute un cours entier et en tire des notes exploitables.

Principe : le navigateur enregistre en continu et envoie l'audio par tranches d'une
minute. Chaque tranche est transcrite (Whisper via Groq, gratuit), puis l'audio est
EFFACÉ immédiatement — seul le texte est conservé.

Pourquoi une condensation intermédiaire alors qu'on ne veut qu'une synthèse finale :
2 h de cours ≈ 18 000 mots ≈ 25 000 tokens. Aucun modèle gratuit n'avale ça d'un coup
proprement. Nova condense donc au fil de l'eau (map), puis bâtit la synthèse finale sur
ces condensés (reduce). L'utilisateur ne voit que le résultat : synthèse + fiches.

Le fichier de session vit dans data/cours/. Comme le disque Render est effacé à chaque
redémarrage, la synthèse finale est AUSSI écrite dans la mémoire de Nova (Supabase si
configuré) et exportable en Markdown.
"""
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)

_DIR = Path("data/cours")
_LOCK = threading.RLock()

# Whisper sur Groq : gratuit, rapide, excellent en français.
# Auto-guérison : si le modèle est retiré, on essaie les suivants (même logique que le chat).
_WHISPER = ("whisper-large-v3-turbo", "whisper-large-v3")
_MODELE_OK = {"nom": ""}

# Condensation dès que la transcription non traitée dépasse ce volume (≈ 15 min de parole).
SEUIL_CONDENSE = 7000        # caractères
# Budget d'entrée pour la synthèse finale : au-delà, on condense les condensés (map-reduce).
BUDGET_FINAL = 12000         # caractères
DUREE_MAX = 4 * 3600         # garde-fou : une session ne peut pas durer plus de 4 h


# ── Persistance ───────────────────────────────────────────────────────────────
def _chemin(sid: str) -> Path:
    return _DIR / f"{_sid_sur(sid)}.json"


def _sid_sur(sid: str) -> str:
    """Un identifiant de session ne peut être qu'un hexadécimal : aucune traversée de dossier."""
    s = re.sub(r"[^a-f0-9]", "", (sid or "").lower())[:32]
    if not s:
        raise ValueError("identifiant de session invalide")
    return s


def _lire(sid: str) -> dict:
    p = _chemin(sid)
    if not p.exists():
        raise KeyError("session inconnue")
    return json.loads(p.read_text(encoding="utf-8"))


def _ecrire(s: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    p = _chemin(s["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)                      # écriture atomique : jamais de fichier à moitié écrit


def demarrer(titre: str = "", matiere: str = "") -> dict:
    """Ouvre une session d'écoute."""
    s = {
        "id": uuid.uuid4().hex,
        "titre": (titre or "").strip()[:120] or f"Cours du {datetime.now():%d/%m/%Y à %Hh%M}",
        "matiere": (matiere or "").strip()[:60],
        "debut": time.time(),
        "fin": None,
        "etat": "en_cours",
        "transcript": "",           # transcription intégrale
        "en_attente": "",           # portion pas encore condensée
        "condenses": [],            # notes intermédiaires (usage interne)
        "segments": 0,              # tranches audio reçues
        "secondes": 0.0,            # durée d'audio réellement transcrite
        "trous": [],                # tranches perdues (réseau) — honnêteté sur les manques
        "synthese": "",
        "fiches": [],
        "erreurs": [],
    }
    with _LOCK:
        _ecrire(s)
    logger.info(f"[cours] session ouverte : {s['titre']}")
    return s


def lister() -> list:
    """Sessions connues, la plus récente d'abord."""
    _DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in _DIR.glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": s["id"], "titre": s["titre"], "matiere": s.get("matiere", ""),
                        "debut": s["debut"], "fin": s.get("fin"), "etat": s.get("etat"),
                        "secondes": s.get("secondes", 0), "mots": len(s.get("transcript", "").split()),
                        "a_synthese": bool(s.get("synthese"))})
        except Exception:
            continue
    return sorted(out, key=lambda x: x["debut"], reverse=True)


def supprimer(sid: str) -> bool:
    p = _chemin(sid)
    if p.exists():
        p.unlink()
        return True
    return False


# ── Transcription ─────────────────────────────────────────────────────────────
def transcription_dispo() -> bool:
    return bool((getattr(config, "GROQ_API_KEY", "") or "").strip())


def _bip_wav(secondes: float = 0.4, taux: int = 16000) -> bytes:
    """Un court son de test, fabriqué en mémoire (aucune dépendance, aucun fichier)."""
    import math
    import struct
    n = int(secondes * taux)
    ech = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / taux)))
                   for i in range(n))
    return (b"RIFF" + struct.pack("<I", 36 + len(ech)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, taux, taux * 2, 2, 16)
            + b"data" + struct.pack("<I", len(ech)) + ech)


_VERIF = {"t": 0.0, "res": None}
_VERIF_TTL = 600.0


def verifier_transcription() -> tuple:
    """Fait un VRAI appel Whisper avant que l'utilisateur n'enregistre.

    Sans ça, une clé expirée ou un modèle retiré ne se découvre qu'à la fin d'un cours
    de 2 h — c'est-à-dire trop tard. On envoie un bip de 0,4 s : si Whisper répond, la
    chaîne complète (clé, modèle, réseau) est prouvée fonctionnelle.
    Retourne (ok, message). Résultat mis en cache 10 minutes.
    """
    if not transcription_dispo():
        return False, ("Ajoute GROQ_API_KEY (gratuite, console.groq.com) pour que Nova "
                       "puisse transcrire tes cours.")
    if _VERIF["res"] is not None and (time.monotonic() - _VERIF["t"]) < _VERIF_TTL:
        return _VERIF["res"]
    try:
        transcrire(_bip_wav(), "test.wav")       # le texte importe peu : c'est un bip
        res = (True, "")
    except Exception as e:
        msg = str(e).lower()
        if "401" in msg or "invalid" in msg or "unauthorized" in msg or "api key" in msg:
            detail = "ta clé GROQ_API_KEY est refusée — régénère-la sur console.groq.com."
        elif "429" in msg or "rate" in msg or "quota" in msg:
            detail = "quota Groq atteint pour le moment — réessaie dans quelques minutes."
        else:
            detail = f"Whisper ne répond pas ({str(e)[:110]})."
        res = (False, "Transcription indisponible : " + detail)
    _VERIF.update(t=time.monotonic(), res=res)
    return res


def transcrire(audio: bytes, nom_fichier: str = "tranche.webm") -> str:
    """Audio → texte via Whisper (Groq). Lève une exception si aucun modèle ne répond.

    L'audio n'est JAMAIS écrit sur le disque : il passe en mémoire et disparaît.
    """
    if not transcription_dispo():
        raise RuntimeError("GROQ_API_KEY absente — la transcription du cours a besoin de cette clé "
                           "gratuite (console.groq.com).")
    from groq import Groq
    from llm.client import TIMEOUT_LLM, _timeout
    client = Groq(api_key=config.GROQ_API_KEY, timeout=_timeout(TIMEOUT_LLM), max_retries=1)

    candidats = [m for m in ((_MODELE_OK["nom"],) + _WHISPER) if m]
    vus, ordre = set(), []
    for m in candidats:
        if m not in vus:
            vus.add(m); ordre.append(m)

    soucis = []
    for modele in ordre:
        try:
            r = client.audio.transcriptions.create(
                file=(nom_fichier, audio),
                model=modele,
                language="fr",
                response_format="json",
                temperature=0.0,
                # Amorce : oriente Whisper vers du vocabulaire de cours plutôt que de la
                # conversation, et l'aide à ponctuer correctement.
                prompt="Transcription d'un cours magistral en français, avec ponctuation.")
            texte = (getattr(r, "text", "") or "").strip()
            _MODELE_OK["nom"] = modele
            return texte
        except Exception as e:
            soucis.append(f"{modele}: {str(e)[:90]}")
            logger.warning(f"[cours] transcription {modele} échouée : {str(e)[:120]}")
    raise RuntimeError("transcription indisponible — " + " | ".join(soucis))


# ── Raccord des tranches ──────────────────────────────────────────────────────
def _mots_nus(txt: str) -> list:
    """(position dans le texte d'origine, mot normalisé) — la ponctuation seule est ignorée.

    Whisper ponctue : « important ! » peut sortir comme deux jetons. Sans filtrer ces
    jetons vides, la comparaison décalait d'un cran et le doublon passait au travers.
    """
    out = []
    for i, m in enumerate((txt or "").split()):
        nu = re.sub(r"[^\wÀ-ÿ']", "", m).lower().strip("'")
        if nu:
            out.append((i, nu))
    return out


def recoller(precedent: str, nouveau: str, fenetre: int = 45) -> str:
    """Retire le chevauchement entre deux tranches consécutives.

    Les tranches se recouvrent volontairement d'une fraction de seconde (le navigateur
    démarre l'enregistreur suivant AVANT d'arrêter le courant) pour ne perdre aucun mot.
    Contrepartie : quelques mots sont transcrits deux fois. On repère ici le plus long
    suffixe de la tranche précédente qui est aussi un préfixe de la nouvelle, et on le coupe.

    La fenêtre est VOLONTAIREMENT large. Le recouvrement mécanique ne fait que 2 ou 3 mots,
    mais Whisper redit parfois toute une phrase de contexte en début de tranche : une fenêtre
    courte laissait alors passer le doublon (constaté sur une simulation de cours de 2 h).
    On garde la correspondance la PLUS LONGUE, donc élargir ne peut qu'améliorer.
    """
    a, b = (precedent or "").strip(), (nouveau or "").strip()
    if not a or not b:
        return b
    ma = [w for _, w in _mots_nus(a)][-fenetre:]
    pb = _mots_nus(b)[:fenetre]
    mb = [w for _, w in pb]
    mots_b = b.split()
    for n in range(min(len(ma), len(mb)), 1, -1):     # ≥ 2 mots : un seul mot est trop fortuit
        if ma[-n:] == mb[:n]:
            coupe = pb[n - 1][0] + 1                  # position réelle dans la tranche d'origine
            return " ".join(mots_b[coupe:]).strip()
    return b


# ── Ingestion d'une tranche ───────────────────────────────────────────────────
def ajouter_tranche(sid: str, audio: bytes, secondes: float = 0.0,
                    nom_fichier: str = "tranche.webm") -> dict:
    """Transcrit une tranche et l'ajoute à la session. L'audio est perdu ensuite : voulu."""
    with _LOCK:
        s = _lire(sid)
        if s.get("etat") != "en_cours":
            raise RuntimeError("cette session est déjà terminée")
        if time.time() - s["debut"] > DUREE_MAX:
            raise RuntimeError("durée maximale d'une session atteinte (4 h)")

    try:
        texte = transcrire(audio, nom_fichier)
    except Exception as e:
        with _LOCK:
            s = _lire(sid)
            s["trous"].append({"segment": s["segments"] + 1, "secondes": round(secondes, 1),
                               "raison": str(e)[:150]})
            s["segments"] += 1
            s["erreurs"] = (s["erreurs"] + [str(e)[:150]])[-5:]
            _ecrire(s)
        raise

    with _LOCK:
        s = _lire(sid)
        # On donne assez de contexte pour que la fenêtre de recollage soit réellement remplie.
        propre = recoller(s["transcript"][-1500:], texte)
        if propre:
            s["transcript"] = (s["transcript"] + " " + propre).strip()
            s["en_attente"] = (s["en_attente"] + " " + propre).strip()
        s["segments"] += 1
        s["secondes"] = round(s.get("secondes", 0.0) + max(0.0, secondes), 1)
        _ecrire(s)
        a_condenser = len(s["en_attente"]) >= SEUIL_CONDENSE

    if a_condenser:
        try:
            _condenser(sid)
        except Exception as e:                       # ne doit JAMAIS interrompre l'écoute
            logger.warning(f"[cours] condensation différée : {str(e)[:120]}")

    with _LOCK:
        s = _lire(sid)
    return {"ok": True, "mots": len(s["transcript"].split()), "segments": s["segments"],
            "secondes": s["secondes"], "trous": len(s["trous"]),
            "apercu": propre[-160:] if propre else ""}


# ── Condensation au fil de l'eau (le « map ») ─────────────────────────────────
_SYS_CONDENSE = (
    "Tu transformes la transcription BRUTE d'un cours en notes fidèles.\n"
    "RÈGLES ABSOLUES :\n"
    "• N'invente RIEN. Si un passage est inaudible ou incohérent, écris « [passage peu clair] ».\n"
    "• Garde les chiffres, dates, formules, noms propres et définitions EXACTEMENT tels quels.\n"
    "• Supprime les hésitations, répétitions et digressions hors sujet.\n"
    "• Conserve les exemples donnés par l'enseignant : ils servent à comprendre.\n"
    "• Signale ce que l'enseignant a insisté (« retenez », « important », « ça tombera »).\n"
    "Rends des notes structurées en Markdown, sans introduction ni conclusion de ta part."
)


def _condenser(sid: str) -> None:
    """Transforme la portion en attente en notes intermédiaires, puis la vide."""
    from llm.client import chat
    with _LOCK:
        s = _lire(sid)
        brut = s["en_attente"].strip()
        if len(brut) < 400:                 # trop court : on garde pour le prochain tour
            return
        s["en_attente"] = ""
        _ecrire(s)                          # on libère tout de suite : l'écoute continue
    try:
        notes = chat([
            {"role": "system", "content": _SYS_CONDENSE},
            {"role": "user", "content": f"Transcription à mettre en notes :\n\n{brut[:14000]}"},
        ], temperature=0.2, niveau="equilibre")
    except Exception:
        with _LOCK:                         # échec : on remet le texte en file, rien n'est perdu
            s = _lire(sid)
            s["en_attente"] = (brut + " " + s["en_attente"]).strip()
            _ecrire(s)
        raise
    with _LOCK:
        s = _lire(sid)
        s["condenses"].append({"t": time.time(), "notes": (notes or "").strip()})
        _ecrire(s)
    logger.info(f"[cours] condensé #{len(s['condenses'])} ({len(brut)} car.)")


def _reduire(blocs: list) -> str:
    """Réduit les condensés jusqu'à tenir dans le budget d'entrée de la synthèse finale.

    Sans ça, un cours de 2 h dépasserait la fenêtre de contexte des modèles gratuits et la
    synthèse serait tronquée — donc inutilisable pour réviser.
    """
    from llm.client import chat
    textes = [b for b in blocs if b and b.strip()]
    garde = 0
    while sum(len(t) for t in textes) > BUDGET_FINAL and len(textes) > 1 and garde < 4:
        garde += 1
        fusion, paquet, taille = [], [], 0
        for t in textes:
            if taille + len(t) > BUDGET_FINAL // 2 and paquet:
                fusion.append("\n\n".join(paquet)); paquet, taille = [], 0
            paquet.append(t); taille += len(t)
        if paquet:
            fusion.append("\n\n".join(paquet))
        textes = []
        for morceau in fusion:
            try:
                textes.append(chat([
                    {"role": "system", "content":
                        "Fusionne ces notes de cours en gardant TOUT ce qui a une valeur "
                        "pédagogique : définitions, formules, chiffres, dates, exemples. "
                        "Supprime uniquement les redites. N'invente rien. Markdown."},
                    {"role": "user", "content": morceau[:14000]},
                ], temperature=0.2, niveau="equilibre") or morceau)
            except Exception:
                textes.append(morceau[:BUDGET_FINAL // 2])   # repli : on tronque plutôt qu'échouer
    return "\n\n".join(textes)[:BUDGET_FINAL]


# ── Synthèse finale + fiches de révision (le « reduce ») ──────────────────────
_SYS_SYNTHESE = (
    "Tu rédiges le cours PROPRE d'un lycéen/étudiant à partir de ses notes de séance.\n"
    "Objectif : qu'il puisse réviser des mois plus tard SANS avoir assisté au cours.\n"
    "RÈGLES ABSOLUES :\n"
    "• N'invente RIEN et n'ajoute aucune connaissance extérieure au cours.\n"
    "• Reproduis les définitions, formules, chiffres et dates À L'IDENTIQUE.\n"
    "• Si un point est incomplet dans les notes, dis-le au lieu de le combler.\n"
    "PLAN IMPOSÉ, en Markdown :\n"
    "## En bref\n(3 à 5 lignes : de quoi parle ce cours)\n"
    "## Le cours\n(le contenu structuré en parties avec des titres ###, "
    "les termes importants en **gras**, les formules dans des blocs de code)\n"
    "## À retenir absolument\n(liste des points que l'enseignant a soulignés)\n"
    "## Zones à éclaircir\n(ce qui était inaudible ou pas clair — écris « rien à signaler » si tout est net)"
)

_SYS_FICHES = (
    "Tu fabriques des fiches de révision (questions/réponses) à partir d'un cours.\n"
    "RÈGLES : réponses tirées UNIQUEMENT du cours fourni, jamais de connaissance extérieure.\n"
    "Chaque question porte sur UN point précis et vérifiable (définition, formule, date, "
    "mécanisme, exemple). Varie les niveaux : restitution, compréhension, application.\n"
    'Réponds en JSON STRICT, sans texte autour : {"fiches":[{"q":"…","r":"…","theme":"…"}]}\n'
    "12 à 20 fiches. « r » fait 1 à 3 phrases. « theme » = 1 à 3 mots."
)


def _fiches_depuis(cours: str) -> list:
    """Fiches Q/R. Retourne une liste vide plutôt que d'échouer : la synthèse prime."""
    from agent.core import _off  # noqa: F401  (import tardif volontaire, évite un cycle)
    from llm.client import chat
    try:
        brut = chat([
            {"role": "system", "content": _SYS_FICHES},
            {"role": "user", "content": cours[:12000]},
        ], temperature=0.3, niveau="puissant") or ""
    except Exception as e:
        logger.warning(f"[cours] fiches indisponibles : {str(e)[:120]}")
        return []
    m = re.search(r"\{.*\}", brut, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for f in (data.get("fiches") or [])[:24]:
        q = str(f.get("q") or "").strip()
        r = str(f.get("r") or "").strip()
        if q and r:
            out.append({"q": q[:300], "r": r[:600], "theme": str(f.get("theme") or "").strip()[:40]})
    return out


def terminer(sid: str) -> dict:
    """Clôture la session : condensation du reste, synthèse finale, fiches de révision."""
    from llm.client import chat
    with _LOCK:
        s = _lire(sid)
        if not s.get("transcript", "").strip():
            s["etat"] = "vide"; s["fin"] = time.time(); _ecrire(s)
            raise RuntimeError("aucune parole n'a été transcrite — rien à synthétiser")
        if s.get("synthese"):                     # déjà fait : on ne refait pas le travail
            return s
        s["etat"] = "traitement"
        _ecrire(s)

    try:
        _condenser(sid)                      # le reliquat en attente
    except Exception as e:
        logger.warning(f"[cours] dernier condensé ignoré : {str(e)[:120]}")

    with _LOCK:
        s = _lire(sid)
    blocs = [c["notes"] for c in s["condenses"] if c.get("notes")]
    if not blocs:                            # cours court : jamais condensé, on part du brut
        blocs = [s["transcript"]]
    if s.get("en_attente", "").strip():      # condensation ratée : on garde quand même le texte
        blocs.append(s["en_attente"])
    matiere = f" de {s['matiere']}" if s.get("matiere") else ""

    try:
        base = _reduire(blocs)
        synthese = (chat([
            {"role": "system", "content": _SYS_SYNTHESE},
            {"role": "user", "content": f"Notes du cours{matiere} « {s['titre']} » :\n\n{base}"},
        ], temperature=0.25, niveau="puissant") or "").strip()
    except Exception as e:
        # ⚠️ On marque « à reprendre », JAMAIS perdu : la transcription reste intacte et
        # l'utilisateur peut relancer la synthèse quand les modèles répondent à nouveau.
        with _LOCK:
            s = _lire(sid)
            s["etat"] = "a_reprendre"
            s["erreurs"] = (s.get("erreurs", []) + [str(e)[:200]])[-5:]
            _ecrire(s)
        raise
    if not synthese:
        with _LOCK:
            s = _lire(sid)
            s["etat"] = "a_reprendre"
            _ecrire(s)
        raise RuntimeError("le modèle n'a rien renvoyé pour la synthèse")

    fiches = _fiches_depuis(synthese)

    with _LOCK:
        s = _lire(sid)
        s["synthese"] = synthese
        s["fiches"] = fiches
        s["etat"] = "termine"
        s["fin"] = time.time()
        _ecrire(s)

    # Le disque Render est effacé à chaque redémarrage : on dépose la synthèse dans la
    # mémoire de Nova (Supabase si configuré) pour qu'elle survive et reste interrogeable.
    try:
        from memory import get_memory
        get_memory().remember("nova", "assistant",
                              f"[cours{matiere}] {s['titre']} — synthèse :\n{synthese[:1500]}")
    except Exception as e:
        logger.warning(f"[cours] mémorisation ignorée : {str(e)[:120]}")

    logger.info(f"[cours] '{s['titre']}' terminé : {len(s['transcript'].split())} mots, "
                f"{len(fiches)} fiches.")
    return s


# ── Export ────────────────────────────────────────────────────────────────────
def markdown(sid: str) -> str:
    """Le cours complet en Markdown : à télécharger et garder hors de Render."""
    s = _lire(sid)
    d = datetime.fromtimestamp(s["debut"])
    mn = int(s.get("secondes", 0) // 60)
    L = [f"# {s['titre']}", ""]
    if s.get("matiere"):
        L.append(f"**Matière :** {s['matiere']}  ")
    L += [f"**Date :** {d:%d/%m/%Y à %Hh%M}  ",
          f"**Durée transcrite :** {mn} min  ",
          f"**Mots :** {len(s.get('transcript', '').split())}", ""]
    if s.get("trous"):
        perdu = sum(t.get("secondes", 0) for t in s["trous"])
        L += [f"> ⚠️ {len(s['trous'])} tranche(s) n'ont pas pu être transcrites "
              f"(≈ {int(perdu)} s manquantes).", ""]
    L += ["---", "", s.get("synthese") or "_Synthèse non générée._", ""]
    if s.get("fiches"):
        L += ["---", "", "## Fiches de révision", ""]
        for i, f in enumerate(s["fiches"], 1):
            t = f" _({f['theme']})_" if f.get("theme") else ""
            L += [f"**{i}. {f['q']}**{t}", "", f"{f['r']}", ""]
    L += ["---", "", "<details><summary>Transcription intégrale</summary>", "",
          s.get("transcript", ""), "", "</details>", ""]
    return "\n".join(L)
