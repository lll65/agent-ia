"""
Envoyer un message sur TON bot Telegram.

⚠️ À la demande « envoie-moi salut sur Telegram », Nova répondait « Je n'ai pas
d'intégration Telegram disponible », puis proposait un tutoriel Make/Zapier —
alors qu'elle a le bot sous la main : c'est par lui qu'elle pousse déjà les
résultats d'automatisation. Elle affirmait donc ne pas savoir faire une chose
qu'elle faisait tous les jours. L'outil manquait simplement à son catalogue.

Volontairement limité au PROPRIÉTAIRE du bot : Nova ne peut écrire qu'à Lohan,
jamais à un tiers. Un message Telegram part sans retour possible, et « envoie un
message à … » ne doit pas pouvoir devenir un canal d'envoi vers n'importe qui.
"""
from plugins.base import Plugin


class TelegramPlugin(Plugin):
    name = "envoyer_telegram"
    description = ("Envoie un message sur le bot Telegram de l'utilisateur (à lui seul). "
                   "Sert à te faire un rappel, un résumé ou une alerte sur son téléphone.")
    parameters = {
        "message": {"type": "string", "description": "Le texte à envoyer", "required": True},
    }

    def run(self, message: str = "", **_) -> str:
        texte = (message or "").strip()
        if not texte:
            return "[ERREUR] Aucun message à envoyer."
        try:
            from bots.telegram_push import send_message, proprietaire, diagnostic
        except Exception as e:
            return f"[ERREUR] Module Telegram indisponible ({type(e).__name__})."
        if not proprietaire("telegram"):
            # On dit ce qui manque et comment y remédier, plutôt que « je ne peux pas ».
            return ("[ERREUR] " + diagnostic().get("resume", "Telegram n'est pas configuré."))
        if send_message(texte[:3900]):
            return "✅ Message envoyé sur ton Telegram."
        return ("[ERREUR] Telegram a refusé l'envoi. Vérifie que le jeton est valide "
                "et que tu n'as pas bloqué le bot.")
