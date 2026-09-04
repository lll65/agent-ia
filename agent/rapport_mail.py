"""
Rapport de mails — quoi lire, quoi répondre, et jamais rien envoyer tout seul.

Ce que Lohan a demandé, mot pour mot :
  • un résumé : lesquels regarder, lesquels demandent une réponse ;
  • pour ceux-là, une réponse PRÉPARÉE et proposée — « mais surtout qu'il ne
    l'envoie pas sans mon autorisation » ;
  • le droit de s'aider des applis connectées pour rédiger (« quelqu'un me
    demande mes dispos » → regarder l'agenda) ;
  • et savoir qu'« une pub Spotify c'est pas important, mais l'abonnement à
    payer, ça l'est ».

⚠️ CE POINT-LÀ EST STRUCTUREL, pas un réglage : ce module ne peut pas envoyer de
mail. Il n'appelle aucune action d'envoi. Les brouillons remontent comme du
texte, et un envoi doit repasser par le garde-fou de confirmation habituel
(api/agent.py, _demande_confirmation). Un mail parti par erreur ne se rattrape
pas.

⚠️ LE TRI EST FAIT PAR DES RÈGLES D'ABORD, le modèle ensuite. « Spotify » apparaît
dans la pub ET dans la facture : c'est le mot « facture », « prélèvement » ou
« échéance » qui tranche, pas l'expéditeur. Confier ce tri au seul modèle, c'est
accepter qu'il rate un paiement un jour de saturation.
"""
import logging
import re

logger = logging.getLogger(__name__)

# ── Ce qui compte VRAIMENT, quel que soit l'expéditeur ────────────────────────
# L'argent et les échéances passent avant tout : les rater coûte cher.
_ARGENT = re.compile(
    r"\b(factur\w+|prélèvement|prelevement|paiement|payer|impay\w+|échéance|echeance|"
    r"rappel de paiement|relance|virement|remboursement|mise en demeure|"
    r"votre commande|abonnement\s+(?:arrive|expire|renouvel\w+)|"
    r"renouvellement|resilia\w+|débit|debit|montant dû|montant du)\b", re.I)
_URGENT = re.compile(
    r"\b(urgent|avant le|au plus tard|deadline|date limite|expire|dernier délai|"
    r"dernier delai|réponse attendue|reponse attendue|convocation|rendez-vous|rdv|"
    r"entretien|inscription|dossier incomplet|pièce manquante|piece manquante)\b", re.I)
_SECURITE = re.compile(
    r"\b(mot de passe|connexion inhabituelle|activité suspecte|activite suspecte|"
    r"vérification|verification|code de sécurité|code de securite|"
    r"double authentification|compte (?:bloqué|bloque|suspendu))\b", re.I)
# Une QUESTION posée appelle une réponse — c'est le cœur du « lesquels répondre ».
_DEMANDE = re.compile(
    r"\b(peux-tu|pourrais-tu|pouvez-vous|est-ce que tu|est-ce que vous|"
    r"tes dispos|vos disponibilités|vos disponibilites|quand es-tu|quand êtes-vous|"
    r"quand etes-vous|merci de me|dis-moi|dites-moi|confirme|confirmez|"
    r"réponds|repondez|réponse de ta part|j'attends ta|j'attends votre)\b", re.I)

# ── Ce qui ne mérite pas qu'on ouvre Gmail ───────────────────────────────────
_PUB = re.compile(
    r"\b(newsletter|se désinscrire|se desinscrire|désabonner|desabonner|"
    r"offre spéciale|offre speciale|promo\w*|soldes|black friday|"
    r"-\s?\d{2}\s?%|\d{2}\s?% de (?:remise|réduction|reduction)|"
    r"ne manquez pas|découvrez notre|decouvrez notre|dernière chance|"
    r"derniere chance|profitez|exclusivité|exclusivite|nouveautés|nouveautes)\b", re.I)
_AUTOMATIQUE = re.compile(r"(no-?reply|ne-pas-repondre|nepasrepondre|donotreply|"
                          r"notification|noreply)", re.I)

# Un mail à la fois publicitaire ET porteur d'argent est IMPORTANT : c'est
# exactement le cas « pub Spotify » contre « abonnement Spotify à payer ».
IMPORTANT, A_LIRE, IGNORER = "important", "a_lire", "ignorer"


def _texte(mail: dict) -> str:
    return " ".join(str(mail.get(c) or "") for c in ("subject", "sujet", "snippet",
                                                     "apercu", "body", "corps"))[:4000]


def _expediteur(mail: dict) -> str:
    return str(mail.get("from") or mail.get("expediteur") or mail.get("sender") or "")


def classer(mail: dict) -> dict:
    """Range un mail, avec la RAISON du classement — jamais un verdict sans motif."""
    t = _texte(mail)
    exp = _expediteur(mail)
    raisons = []

    if _ARGENT.search(t):
        raisons.append("argent ou échéance de paiement")
    if _SECURITE.search(t):
        raisons.append("sécurité du compte")
    if _URGENT.search(t):
        raisons.append("date limite ou rendez-vous")
    repondre = bool(_DEMANDE.search(t)) and not _AUTOMATIQUE.search(exp)
    if repondre:
        raisons.append("on te pose une question")

    if raisons:
        # ⚠️ Même si ça ressemble à de la pub : une facture reste une facture.
        return {"niveau": IMPORTANT, "repondre": repondre,
                "pourquoi": " · ".join(raisons)}
    if _PUB.search(t) or _AUTOMATIQUE.search(exp):
        return {"niveau": IGNORER, "repondre": False,
                "pourquoi": "publicité ou envoi automatique, rien à faire"}
    return {"niveau": A_LIRE, "repondre": False,
            "pourquoi": "ni urgent ni publicitaire — à lire quand tu as le temps"}


def a_besoin_agenda(mail: dict) -> bool:
    """Ce mail demande-t-il des disponibilités ? (→ Nova va regarder l'agenda)

    C'est le cas que Lohan a cité : « quelqu'un me demande mes dispos ». Répondre
    sans regarder le calendrier, ce serait inventer.
    """
    return bool(re.search(
        r"\b(dispo\w*|disponibilit\w+|créneau|creneau|quand (?:es-tu|êtes-vous|etes-vous|"
        r"peux-tu|pouvez-vous)|libre|ton agenda|votre agenda|caler|fixer un|"
        r"proposer une date|quelle date|quel jour|quelle heure)\b",
        _texte(mail), re.I))


def trier(mails: list) -> dict:
    """Le rapport complet, range par ce qu'il y a a FAIRE."""
    out = {IMPORTANT: [], A_LIRE: [], IGNORER: []}
    for m in (mails or []):
        if not isinstance(m, dict):
            continue
        c = classer(m)
        out[c["niveau"]].append({**m, **c, "agenda": a_besoin_agenda(m)})
    return out


def resume_markdown(tri: dict, brouillons: dict = None) -> str:
    """Le rapport tel que Lohan le lit : d'abord ce qui demande une action."""
    brouillons = brouillons or {}
    L = []
    imp = tri.get(IMPORTANT) or []
    lire = tri.get(A_LIRE) or []
    rien = tri.get(IGNORER) or []

    if not (imp or lire or rien):
        return "📭 Aucun mail à traiter."

    a_repondre = [m for m in imp if m.get("repondre")]
    autres_imp = [m for m in imp if not m.get("repondre")]

    if a_repondre:
        L.append(f"### ✍️ À répondre ({len(a_repondre)})")
        for m in a_repondre:
            L.append(f"- **{_sujet(m)}** — {_court(_expediteur(m))}")
            L.append(f"  - _{m['pourquoi']}_")
            b = brouillons.get(_cle(m))
            if b:
                L.append("")
                L.append(f"  > {b}")
                L.append("")
                # ⚠️ Ce bloc devient des BOUTONS dans l'interface (voir fmt() dans
                # ui/nova.html). Il est écrit ici, en Python, et pas laissé au modèle :
                # une proposition d'envoi doit TOUJOURS s'accompagner de son choix,
                # pas seulement quand le modèle y pense.
                qui = _court(_expediteur(m))
                L.append(f"CHOIX: Envoyer cette réponse à {qui} ?")
                L.append(f"- Envoyer la réponse à {qui}")
                L.append("- Modifier la réponse")
                L.append("- Laisser ce mail de côté")
                L.append("")
        L.append("")
    if autres_imp:
        L.append(f"### 🔴 Important, sans réponse attendue ({len(autres_imp)})")
        for m in autres_imp:
            L.append(f"- **{_sujet(m)}** — {_court(_expediteur(m))} · _{m['pourquoi']}_")
        L.append("")
    if lire:
        L.append(f"### 📄 À lire quand tu as le temps ({len(lire)})")
        for m in lire[:8]:
            L.append(f"- {_sujet(m)} — {_court(_expediteur(m))}")
        L.append("")
    if rien:
        # On ne les liste pas : les nommer un par un, c'est recréer la boîte mail.
        L.append(f"### 🗑️ Ignorés ({len(rien)})")
        L.append("Publicités et envois automatiques — rien à y faire.")
    return "\n".join(L)


def _sujet(m: dict) -> str:
    return (str(m.get("subject") or m.get("sujet") or "(sans objet)")).strip()[:90]


def _court(exp: str) -> str:
    """« Marie Dupont <marie@x.fr> » → « Marie Dupont »."""
    exp = (exp or "").strip()
    nom = re.sub(r"<[^>]*>", "", exp).strip(' "\'')
    return (nom or exp)[:40] or "expéditeur inconnu"


def _cle(m: dict) -> str:
    return str(m.get("id") or m.get("messageId") or _sujet(m))

# ═══ LA VERSION PARLÉE ═══
#
# ⚠️ « En vocal faut pas qu'elle écrive tout, mais qu'elle interagisse avec moi —
# genre elle me dit : tu veux les mails importants ou pas ? »
# À l'écran, Nova affichait « ### 🔴 Important, sans réponse attendue (2) - ** 🚨 Marta
# Ferrando Merino vient de publier une story qui exp… » : le rapport ÉCRIT, en markdown
# brut, coupé en plein mot. Et elle le lisait à voix haute tel quel.
# Parler et écrire ne demandent pas le même texte. À l'oral on dit l'essentiel en deux
# phrases et on POSE UNE QUESTION — on ne récite pas un document.
def resume_vocal(tri: dict) -> str:
    """Ce que Nova DIT quand on lui demande ses mails à la voix. Deux phrases, une question."""
    imp = tri.get(IMPORTANT) or []
    lire = tri.get(A_LIRE) or []
    rien = tri.get(IGNORER) or []
    total = len(imp) + len(lire) + len(rien)
    if not total:
        return "Tu n'as aucun mail à traiter pour l'instant."

    a_repondre = [m for m in imp if m.get("repondre")]
    bouts = []
    if a_repondre:
        qui = _court(_expediteur(a_repondre[0]))
        bouts.append(f"{_nombre(len(a_repondre))} qui {'attendent' if len(a_repondre) > 1 else 'attend'} "
                     f"une réponse, dont {'un de ' + qui if qui != 'expéditeur inconnu' else 'un'}")
    autres = [m for m in imp if not m.get("repondre")]
    if autres:
        bouts.append(f"{_nombre(len(autres))} {'importants' if len(autres) > 1 else 'important'} "
                     "sans réponse attendue")
    if rien:
        bouts.append(f"{_nombre(len(rien))} de publicité que j'ai mis de côté")

    phrase = f"Tu as {_nombre(total)} mail{'s' if total > 1 else ''}"
    if bouts:
        phrase += " : " + ", ".join(bouts[:2]) + "."
    else:
        phrase += "."

    # La question — c'est elle qui fait une conversation plutôt qu'un bulletin.
    if a_repondre:
        question = "Tu veux que je te lise ceux qui attendent une réponse ?"
    elif imp:
        question = "Tu veux que je te dise lesquels sont importants ?"
    else:
        question = "Tu veux que je t'en lise un ?"
    return phrase + " " + question


_UNITES = ("zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit",
           "neuf", "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize")


def _nombre(n: int) -> str:
    """« 3 » se lit mal quand la voix hésite : on l'écrit en toutes lettres."""
    return _UNITES[n] if 0 <= n < len(_UNITES) else str(n)
