"""
L'heure de l'utilisateur — pas celle du serveur.

⚠️ Le conteneur Render tourne en UTC. Tous les planificateurs utilisaient
`datetime.now()`, c'est-à-dire l'heure UTC : « briefing du matin à 7h » partait donc à
9h heure de Paris en été, « préparation du lendemain à 21h » à 23h, et le bilan
hebdomadaire du dimanche soir pouvait basculer sur le mauvais jour. Décalage invisible
dans le code, très visible pour celui qui attend son briefing.

Une seule source de vérité ici, réglable par la variable FUSEAU.
"""
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

FUSEAU = os.getenv("FUSEAU", "Europe/Paris")


def zone():
    """Le fuseau configuré, ou None si la base de données de fuseaux manque."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(FUSEAU)
    except Exception as e:      # zoneinfo absent, ou nom de fuseau invalide
        logger.warning(f"[horloge] fuseau « {FUSEAU} » indisponible ({e}) — heure du serveur.")
        return None


def maintenant() -> datetime:
    """L'heure qu'il est CHEZ L'UTILISATEUR (naïve, pour comparer heures et jours)."""
    z = zone()
    return datetime.now(z).replace(tzinfo=None) if z else datetime.now()


def decalage_h() -> float:
    """Écart entre l'heure locale de l'utilisateur et celle du serveur, en heures.

    Sert au diagnostic : c'est ce nombre qui explique un briefing reçu 2 h trop tard.
    """
    ecart = (maintenant() - datetime.now()).total_seconds() / 3600
    return round(ecart, 1)
