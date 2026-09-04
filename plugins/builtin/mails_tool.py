"""
Rapport de mails — lit, trie, prépare des réponses, et n'envoie RIEN.

⚠️ Ce plugin n'appelle AUCUNE action d'envoi. Structurellement, pas par réglage :
la seule action Gmail utilisée est la LECTURE. Un envoi doit repasser par le
chemin normal, qui exige une confirmation écrite (voir _demande_confirmation dans
api/agent.py). « Tu te rends compte s'il fait ça avec mes mails ! » — un mail
parti par erreur ne se rattrape pas, donc aucun raccourci ici.
"""
import json
import logging
import re

from plugins.base import Plugin

logger = logging.getLogger(__name__)

# Actions de LECTURE seulement. Toute action d'envoi est volontairement absente.
_LECTURE = "GMAIL_FETCH_EMAILS"


class RapportMailsPlugin(Plugin):
    name = "rapport_mails"
    description = ("Résume tes mails : lesquels regarder, lesquels demandent une "
                   "réponse, et propose un brouillon pour ceux-là. N'envoie jamais "
                   "rien — il faut ton accord explicite.")
    parameters = {
        "combien": {"type": "integer", "description": "Nombre de mails à examiner (défaut 20)",
                    "required": False},
        "non_lus": {"type": "boolean", "description": "Seulement les non lus (défaut oui)",
                    "required": False},
    }

    def run(self, combien: int = 20, non_lus: bool = True, vocal: bool = False, **_) -> str:
        try:
            from api.agent import _tool
            from agent.rapport_mail import trier, resume_markdown, a_besoin_agenda, _cle
        except Exception as e:
            return f"[ERREUR] Module indisponible ({type(e).__name__})."

        # ⚠️ J'avais écrit « max_results » et retiré la requête. L'ancien appel, qui
        # MARCHAIT, utilisait « maxResults » et « in:inbox » : Composio ignorait donc
        # mes paramètres et je concluais « aucun mail » sur une boîte pleine. On reprend
        # exactement la forme éprouvée.
        args = {"maxResults": max(1, min(50, int(combien or 20))),
                "query": "is:unread" if non_lus else "in:inbox"}
        brut = _tool(_LECTURE, args, "gmail")
        # ⚠️ L'ÉCHEC se juge AVANT l'extraction. `{"successful": false, "error": "401"}`
        # est un JSON parfaitement valide : extrait, il devenait « un mail » de plus,
        # classé et présenté comme tel. On ne prétend pas non plus « aucun mail » quand
        # c'est l'accès qui a échoué — les deux se ressemblent, et les confondre
        # reviendrait à rassurer à tort sur une boîte qu'on n'a pas pu lire.
        if _echec(str(brut)):
            return ("[ERREUR] Je n'ai pas pu lire ta boîte — je ne te dis donc PAS "
                    f"que tu n'as rien.\n\n_Détail : {str(brut)[:220]}_")
        mails = _extraire(str(brut))
        if not mails:
            # ⚠️ « 📭 Aucun mail à traiter » s'affichait sur une boîte PLEINE, et rien ne
            # permettait de savoir si la boîte était vide ou si l'extraction avait raté.
            # Les deux se lisent pareil et n'ont rien à voir. Gmail renvoie toujours une
            # enveloppe : si elle est grosse, c'est qu'il y avait des mails dedans et que
            # c'est MOI qui n'ai pas su les lire. On le dit, avec de quoi le prouver.
            taille = len(str(brut or ""))
            if taille > 400:
                logger.warning(f"[rapport_mails] réponse de {taille} octets, 0 mail extrait")
                return ("⚠️ **Gmail m'a répondu, mais je n'ai pas su lire ses mails.** "
                        "Je ne te dis donc PAS que ta boîte est vide — je ne le sais pas.\n\n"
                        f"_Réponse reçue : {taille} octets, aucun message reconnu. "
                        "Envoie-moi cette ligne, c'est le format de Composio qui a changé._\n\n"
                        f"_Début de la réponse : {str(brut)[:200]}_")
            return ("📭 Aucun mail à traiter — Gmail n'a renvoyé aucun message"
                    + (" non lu." if non_lus else " dans ta boîte de réception."))

        tri = trier(mails)
        # ⚠️ À la voix, on ne récite pas un rapport : on dit l'essentiel et on POSE UNE
        # QUESTION. Et surtout on ne prépare PAS de brouillons — c'est un appel modèle
        # par mail, donc plusieurs secondes de silence avant qu'elle ouvre la bouche.
        if vocal:
            from agent.rapport_mail import resume_vocal
            return resume_vocal(tri)
        # Les réponses ne sont préparées que pour ceux qui en attendent une.
        brouillons = {}
        for m in tri.get("important", []):
            if not m.get("repondre"):
                continue
            contexte = ""
            if a_besoin_agenda(m):
                # ⚠️ Répondre « je suis libre mardi » sans regarder l'agenda, ce serait
                # inventer une disponibilité. On va la chercher.
                contexte = _dispos()
            b = _brouillon(m, contexte)
            if b:
                brouillons[_cle(m)] = b

        rapport = resume_markdown(tri, brouillons)
        # ⚠️ Ce pied de page était INCONDITIONNEL. Sur un rapport SANS aucun brouillon,
        # il annonçait « les réponses ci-dessus sont des propositions : dis-moi laquelle
        # envoyer » — en parlant de réponses qui n'existaient pas. Une phrase rassurante
        # qui décrit autre chose que ce qu'on a sous les yeux use la confiance aussi
        # sûrement qu'une erreur franche.
        if brouillons:
            return rapport + (
                "\n\n---\n🔒 **Aucun mail n'a été envoyé, supprimé ni archivé.** "
                "Les réponses ci-dessus sont des propositions : dis-moi laquelle envoyer, "
                "et je te redemanderai confirmation avant de le faire."
            )
        return rapport + "\n\n---\n🔒 _Je n'ai rien envoyé, supprimé ni archivé — j'ai lu, c'est tout._"


def _echec(brut: str) -> bool:
    b = (brut or "").lower()
    return any(k in b for k in ("[erreur]", "no connected account", "401", "403",
                                "unauthorized", "forbidden", '"successful": false'))


# Ce qui fait qu'un objet EST un mail. Composio change de forme d'une version à
# l'autre : on reconnaît donc le contenu, pas l'emballage.
_CHAMPS_MAIL = ("subject", "sujet", "snippet", "apercu", "preview", "from",
                "expediteur", "sender", "body", "corps", "messageText", "payload")


def _est_mail(x) -> bool:
    return isinstance(x, dict) and any(x.get(c) for c in _CHAMPS_MAIL)


def _extraire(brut: str) -> list:
    """Les mails, quelle que soit la forme que Composio a renvoyée.

    ⚠️ L'ancienne version descendait par une liste FIXE de clés (« data », « messages »…)
    et abandonnait dès qu'aucune ne correspondait — d'où « 📭 Aucun mail à traiter »
    sur une boîte pleine, simplement parce que l'emballage avait changé de nom. On
    cherche maintenant N'IMPORTE OÙ dans la structure le premier ensemble d'objets qui
    ressemblent à des mails.
    """
    racine = _json_dans(brut)
    if racine is None:
        # ⚠️ La réponse peut arriver TRONQUÉE (bornée en amont) : plus rien ne parse en
        # entier. On récupère alors les mails un par un — mieux vaut en rendre neuf sur
        # dix que zéro sur dix.
        return _objets_mails(brut)

    trouve = []

    def explore(n, prof=0):
        if trouve or prof > 6:
            return
        if isinstance(n, list):
            mails = [x for x in n if _est_mail(x)]
            if mails:
                trouve.extend(mails[:50])
                return
            for x in n[:50]:
                explore(x, prof + 1)
        elif isinstance(n, dict):
            for v in n.values():
                explore(v, prof + 1)
                if trouve:
                    return

    explore(racine)
    # Un objet unique peut être un mail à lui seul.
    if not trouve and _est_mail(racine):
        trouve = [racine]
    # ⚠️ Sur une réponse COUPÉE, l'enveloppe ne parse plus : _json_dans tombe alors sur
    # le premier mail complet et s'arrête là — un mail rendu au lieu de neuf. On compte
    # donc les deux méthodes et on garde la plus généreuse. Rendre moins que ce qu'on
    # peut lire, c'est la même faute que de ne rien rendre du tout, en plus discret.
    secours = _objets_mails(brut)
    if len(secours) > len(trouve):
        return secours[:50]
    return trouve[:50]


def _json_dans(brut: str):
    """Le premier objet JSON RÉELLEMENT décodable du texte. None si aucun.

    ⚠️ LA cause de « 📭 Aucun mail à traiter » sur une boîte pleine, trouvée grâce au
    message de diagnostic. On cherchait le JSON avec `re.search(r"\\{.*\\}|\\[.*\\]")`.
    Or l'observation commence par « ✅ [GMAIL_FETCH_EMAILS] résultat : {…} » : à la
    position du crochet de GMAIL_FETCH_EMAILS, la première alternative échoue, la
    seconde attrape TOUT jusqu'au dernier « ] » de la réponse — soit
    « [GMAIL_FETCH_EMAILS] résultat : {… » , qui n'est évidemment pas du JSON. Le
    nom de l'action décidait donc si Nova savait lire tes mails.
    On ne devine plus où commence le JSON : on essaie de le DÉCODER à chaque début
    possible, et le premier qui tient est le bon.
    """
    dec = json.JSONDecoder()
    texte = brut or ""
    for i, c in enumerate(texte):
        if c not in "{[":
            continue
        try:
            valeur, _ = dec.raw_decode(texte, i)
        except ValueError:
            continue
        if isinstance(valeur, (dict, list)):
            return valeur
    return None


def _objets_mails(brut: str) -> list:
    """Les objets qui ressemblent à des mails, ramassés un par un.

    Dernier recours, utile quand la réponse est coupée en plein milieu : chaque « { »
    est tenté séparément, et on garde ce qui est à la fois valide et mail-like.
    """
    dec, texte, out = json.JSONDecoder(), brut or "", []
    i = texte.find("{")
    while i != -1 and len(out) < 50:
        try:
            valeur, fin = dec.raw_decode(texte, i)
        except ValueError:
            i = texte.find("{", i + 1)
            continue
        if _est_mail(valeur):
            out.append(valeur)
            i = texte.find("{", fin)
        else:
            i = texte.find("{", i + 1)
    return out


def _dispos() -> str:
    """Les créneaux réellement libres, lus dans l'agenda."""
    try:
        from api.agent import _tool
        from datetime import timedelta
        from agent.horloge import maintenant
        now = maintenant()
        obs = _tool("GOOGLECALENDAR_EVENTS_LIST", {
            "calendarId": "primary", "singleEvents": True, "orderBy": "startTime",
            "timeMin": now.isoformat() + "Z",
            "timeMax": (now + timedelta(days=10)).isoformat() + "Z",
            "maxResults": 30}, "googlecalendar")
        return f"AGENDA RÉEL des 10 prochains jours :\n{str(obs)[:1800]}"
    except Exception as e:
        logger.info(f"[rapport_mails] agenda indisponible ({type(e).__name__})")
        return ""


def _brouillon(mail: dict, contexte: str = "") -> str:
    """Une réponse PROPOSÉE — jamais envoyée."""
    try:
        from llm.client import chat
        from agent.rapport_mail import _texte, _court, _expediteur
        consigne = (
            "Rédige une réponse COURTE (2 à 4 phrases) au mail ci-dessous, en français, "
            "à la première personne, ton naturel et poli. "
            "N'invente AUCUNE information : si tu ne sais pas, écris explicitement ce "
            "qu'il reste à compléter entre crochets, par exemple [à confirmer]. "
            "Pas de formule pompeuse, pas de signature.")
        if contexte:
            consigne += (" Les disponibilités ci-jointes viennent de son VRAI agenda : "
                         "propose des créneaux réellement libres, et rien d'autre.")
        out = chat([
            {"role": "system", "content": consigne},
            {"role": "user", "content": (f"De : {_court(_expediteur(mail))}\n"
                                         f"Objet : {mail.get('subject') or ''}\n"
                                         f"Message : {_texte(mail)[:1200]}\n\n"
                                         + (contexte or ""))},
        ], temperature=0.3)
        from agent.core import sans_raisonnement
        return " ".join(sans_raisonnement(out or "").split())[:600]
    except Exception as e:
        logger.info(f"[rapport_mails] brouillon impossible ({type(e).__name__})")
        return ""
