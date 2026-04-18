"""
Exemple de plugin custom — copie ce fichier et modifie-le pour créer tes propres outils.
Place tes plugins dans data/plugins/ et ils seront chargés automatiquement.
"""
from plugins.base import Plugin


class HelloPlugin(Plugin):
    name = "hello"
    description = "Exemple de plugin custom — retourne un message de salutation."
    parameters = {
        "name": {"type": "string", "description": "Nom de la personne", "required": True},
        "lang": {"type": "string", "description": "Langue: fr|en|es", "required": False},
    }

    def run(self, name: str, lang: str = "fr") -> str:
        greetings = {"fr": "Bonjour", "en": "Hello", "es": "Hola"}
        greeting = greetings.get(lang, "Bonjour")
        return f"{greeting}, {name}! Je suis un plugin custom chargé dynamiquement."
