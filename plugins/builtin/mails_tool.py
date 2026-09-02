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

    def run(self, combien: int = 20, non_lus: bool = True, **_) -> str:
        try:
            from api.agent import _tool
            from agent.rapport_mail import trier, resume_markdown, a_besoin_agenda, _cle
        except Exception as e:
            return f"[ERREUR] Module indisponible ({type(e).__name__})."

        args = {"max_results": max(1, min(50, int(combien or 20)))}
        if non_lus:
            args["query"] = "is:unread"
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
            return "📭 Aucun mail à traiter."

        tri = trier(mails)
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
        return rapport + (
            "\n\n---\n🔒 **Aucun mail n'a été envoyé, supprimé ni archivé.** "
            "Les réponses ci-dessus sont des propositions : dis-moi laquelle envoyer, "
            "et je te redemanderai confirmation avant de le faire."
        )


def _echec(brut: str) -> bool:
    b = (brut or "").lower()
    return any(k in b for k in ("[erreur]", "no connected account", "401", "403",
                                "unauthorized", "forbidden", '"successful": false'))


def _extraire(brut: str) -> list:
    """Les mails, quelle que soit la forme que Composio a renvoyée."""
    m = re.search(r"\{.*\}|\[.*\]", brut or "", re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except Exception:
        return []
    for _ in range(5):
        if isinstance(d, dict):
            for c in ("data", "response_data", "messages", "emails", "items", "result"):
                if isinstance(d.get(c), (list, dict)):
                    d = d[c]
                    break
            else:
                break
        else:
            break
    if isinstance(d, dict):
        d = [d]
    if not isinstance(d, list):
        return []
    # Un objet n'est un mail que s'il en a les traits. Sans ce filtre, une enveloppe
    # de statut ou un objet de pagination se retrouvait classé comme un message.
    champs = ("subject", "sujet", "snippet", "apercu", "from", "expediteur",
              "sender", "body", "corps")
    return [x for x in d
            if isinstance(x, dict) and any(x.get(c) for c in champs)][:50]


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
