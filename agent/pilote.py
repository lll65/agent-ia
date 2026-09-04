"""
Nova pilote un navigateur DÉDIÉ sur le PC de Lohan — et il voit tout se faire.

« Est-ce que Nova peut prendre le contrôle de ma souris et de mon ordi, et que je voie
en direct ma souris bouger toute seule, aller sur Google, ouvrir des apps ? »

Réponse : oui, mais on commence par un NAVIGATEUR DÉDIÉ. Ce module tient la partie
qui décide ; le programme qui bouge réellement le curseur est `pilote/nova_pilote.py`,
et il tourne sur SON PC — Nova, elle, est sur un serveur à l'autre bout d'Internet et
n'a aucun accès à son écran.

⚠️ POURQUOI CE MODULE EXISTE SÉPARÉMENT DU RESTE.
Tout ce qu'on a construit jusqu'ici protège des actions passant par des API : Nova
« demande » à Gmail d'envoyer, et le garde-fou peut refuser. Un navigateur piloté ne
demande rien à personne : il CLIQUE sur « Envoyer ». Aucune des protections existantes
ne s'y applique. Il faut donc des règles à lui, et elles sont écrites ici, en dur,
plutôt que confiées au modèle — c'est exactement la leçon de la journée : ce qui coûte
cher ne se met pas dans un prompt.

LES QUATRE RÈGLES, dans l'ordre d'importance :
  1. Un navigateur DÉDIÉ, profil vierge. Jamais son Chrome habituel : sans ses cookies,
     Nova n'est connectée à rien — ni à sa banque, ni à ses mails, ni à ses réseaux.
     C'est la protection la plus solide, parce qu'elle ne dépend d'aucune vérification.
  2. Des domaines INTERDITS en dur : banque, paiement, administration. Même si le
     modèle s'égare, même si une page l'y pousse, le pilote refuse d'y aller.
  3. Des gestes INTERDITS : jamais de saisie dans un champ de mot de passe, jamais de
     validation de paiement, jamais de téléchargement lancé tout seul.
  4. Il VOIT tout et peut tout arrêter. Chaque geste est annoncé avant d'être fait,
     avec un délai pour le lire, et une touche coupe tout.
"""
import logging
import re
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── 2. Là où le pilote n'ira JAMAIS ──────────────────────────────────────────
# Écrit en dur, pas en réglage : un réglage se change, y compris par erreur. La liste
# est volontairement large — mieux vaut refuser un site anodin que d'en autoriser un
# où une fausse manœuvre coûte de l'argent.
_DOMAINES_INTERDITS = (
    # Banques et courtiers
    "banque", "bank", "credit-agricole", "creditagricole", "bnpparibas", "societegenerale",
    "caisse-epargne", "caisseepargne", "labanquepostale", "boursorama-banque", "fortuneo",
    "hellobank", "revolut", "n26", "lydia", "bourso", "trade-republic", "traderepublic",
    "degiro", "saxobank", "interactivebrokers", "binance", "coinbase", "kraken",
    # Paiement
    "paypal", "stripe.com", "checkout", "paiement", "payment", "3dsecure", "systempay",
    "paylib", "klarna", "alma.eu",
    # Administratif et identité
    "impots.gouv", "ameli.fr", "franceconnect", "service-public.fr", "urssaf",
    "ants.gouv", "caf.fr", "pole-emploi", "francetravail",
)

# ── 3. Les gestes que le pilote ne fera jamais ───────────────────────────────
_CHAMPS_SENSIBLES = re.compile(
    r"(mot de passe|password|passwd|code secret|code pin|cvv|cvc|"
    r"num[ée]ro de carte|card number|iban|secret|otp|code de v[ée]rification)", re.I)
_TEXTES_INTERDITS = re.compile(
    r"(payer|acheter maintenant|valider le paiement|confirmer la commande|"
    r"virement|transf[ée]rer|supprimer mon compte|se d[ée]sinscrire de tout)", re.I)

# Les seuls gestes que le pilote sait faire. Tout le reste est refusé — une liste
# blanche se raisonne, une liste noire s'oublie.
GESTES = ("ouvrir", "clic", "ecrire", "defiler", "attendre", "lire", "capture")


class Refus(Exception):
    """Un ordre que le pilote refuse d'exécuter, avec sa raison en clair."""


def _domaine(url: str) -> str:
    try:
        h = (urlparse(url if "://" in str(url) else "https://" + str(url)).hostname or "")
        return h.lower()
    except Exception:
        return ""


# ⚠️ Trouvé par le test : ne regarder que le DOMAINE laissait passer
# « x.fr/paiement/valider ». Une page de paiement vit le plus souvent dans le CHEMIN,
# pas dans le nom de domaine — c'est même le cas le plus courant sur les boutiques.
_CHEMINS_INTERDITS = ("/paiement", "/payment", "/checkout", "/3dsecure", "/virement",
                      "/transfer", "/pay/", "/carte-bancaire", "/cb-", "/order/pay",
                      "/commande/payer", "/souscription", "/mandat")


def site_interdit(url: str) -> str:
    """Le motif d'interdiction, ou "" si l'adresse est autorisée."""
    u = str(url or "").lower()
    d = _domaine(u)
    if not d:
        return ""
    for motif in _DOMAINES_INTERDITS:
        if motif in d:
            return motif
    # Le chemin compte autant que le domaine.
    chemin = u.split(d, 1)[-1] if d in u else u
    for motif in _CHEMINS_INTERDITS:
        if motif in chemin:
            return motif.strip("/")
    return ""


def verifie(geste: dict) -> dict:
    """Valide UN geste. Lève Refus avec une raison lisible, sinon rend le geste propre.

    ⚠️ Cette fonction est le seul endroit qui autorise. Le programme local la rejoue de
    son côté : si le serveur était compromis ou le modèle égaré, le PC refuserait quand
    même. Deux vérifications valent mieux qu'une quand la seconde est gratuite.
    """
    if not isinstance(geste, dict):
        raise Refus("un ordre doit être un objet, pas " + type(geste).__name__)
    quoi = str(geste.get("quoi") or "").strip().lower()
    if quoi not in GESTES:
        raise Refus(f"je ne sais pas faire « {quoi or '(vide)'} » — "
                    f"gestes possibles : {', '.join(GESTES)}")
    cible = str(geste.get("cible") or "").strip()
    valeur = str(geste.get("valeur") or "")

    if quoi == "ouvrir":
        if not cible:
            raise Refus("ouvrir quoi ? aucune adresse donnée")
        motif = site_interdit(cible)
        if motif:
            raise Refus(f"je refuse d'aller sur un site de ce type (« {motif} ») — "
                        "banque, paiement et administration sont interdits au pilote")
        if not re.match(r"^https?://", cible):
            cible = "https://" + cible
    if quoi == "ecrire":
        if _CHAMPS_SENSIBLES.search(cible) or _CHAMPS_SENSIBLES.search(valeur):
            raise Refus("je ne tape jamais un mot de passe, un code ou un numéro de "
                        "carte — même si tu me le demandes")
    if quoi == "clic" and _TEXTES_INTERDITS.search(cible):
        raise Refus(f"je ne clique pas sur « {cible} » : c'est une action qui engage "
                    "de l'argent ou qui ne se rattrape pas")
    return {"quoi": quoi, "cible": cible, "valeur": valeur,
            "pourquoi": str(geste.get("pourquoi") or "")[:200]}


def verifie_plan(gestes) -> list:
    """Valide un plan entier. Le premier refus arrête tout : on n'exécute pas à moitié.

    ⚠️ Un plan à moitié joué laisse le navigateur dans un état que personne n'a voulu —
    une recherche lancée, un formulaire à demi rempli. Mieux vaut ne rien commencer.
    """
    plan = list(gestes or [])
    if not plan:
        raise Refus("plan vide")
    if len(plan) > 25:
        raise Refus(f"plan trop long ({len(plan)} étapes) — je m'arrête à 25, "
                    "au-delà tu ne peux plus suivre ce qui se passe")
    return [verifie(g) for g in plan]


def resume(plan: list) -> str:
    """Le plan en français, pour qu'il sache à quoi il dit oui AVANT que ça bouge."""
    lignes = []
    for i, g in enumerate(plan or [], 1):
        q, c, v = g.get("quoi"), g.get("cible", ""), g.get("valeur", "")
        if q == "ouvrir":
            lignes.append(f"{i}. ouvrir **{c}**")
        elif q == "clic":
            lignes.append(f"{i}. cliquer sur « {c} »")
        elif q == "ecrire":
            lignes.append(f"{i}. écrire « {v} »" + (f" dans « {c} »" if c else ""))
        elif q == "defiler":
            lignes.append(f"{i}. faire défiler la page")
        elif q == "attendre":
            lignes.append(f"{i}. attendre {c or '2'} s")
        elif q == "lire":
            lignes.append(f"{i}. lire le contenu de la page")
        elif q == "capture":
            lignes.append(f"{i}. prendre une capture d'écran")
    return "\n".join(lignes)


# ── La file d'attente entre Nova et le programme local ───────────────────────
# ⚠️ En mémoire, volontairement. Un ordre de pilotage ne doit PAS survivre à un
# redémarrage du serveur : Lohan n'aurait aucune raison de s'attendre à voir sa souris
# bouger pour une demande faite avant une coupure. Ce qui commande son écran doit être
# aussi éphémère que sa présence devant.
_FILE = []
_RESULTATS = []
_TTL = 120.0                       # au-delà de deux minutes, l'ordre est périmé


def depose(plan: list, demande: str = "") -> dict:
    """Met un plan VALIDÉ en attente que le programme local vienne le chercher."""
    propre = verifie_plan(plan)
    lot = {"id": f"p{int(time.time() * 1000)}", "gestes": propre,
           "demande": (demande or "")[:200], "t": time.time()}
    _FILE.append(lot)
    logger.info(f"[pilote] plan déposé : {len(propre)} geste(s) pour « {lot['demande'][:60]} »")
    return lot


def prochain() -> dict:
    """Ce que le programme local doit faire maintenant. {} s'il n'y a rien."""
    maintenant = time.time()
    while _FILE:
        lot = _FILE.pop(0)
        if maintenant - lot["t"] <= _TTL:
            return lot
        logger.info(f"[pilote] plan {lot['id']} périmé, jeté sans être joué")
    return {}


def enregistre(resultat: dict) -> None:
    """Ce que le programme local a VRAIMENT fait — succès comme échec."""
    _RESULTATS.append({**(resultat or {}), "t": time.time()})
    del _RESULTATS[:-20]


def derniers(n: int = 5) -> list:
    return list(_RESULTATS[-n:])


def en_attente() -> int:
    return len(_FILE)
