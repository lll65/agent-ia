"""
D'où vient le travail en cours : le chat, la passerelle, ou une automatisation de nuit.

⚠️ CE N'EST PAS UNE QUESTION DE CONFORT, C'EST LE GARDE-FOU DES ACTIONS SANS RETOUR.

Le refus « en mode fond, personne n'est là pour confirmer, donc je n'arme rien » ne
vaut que si l'on sait DE FAÇON FIABLE qu'on est en mode fond. Or le canal était rangé
dans un simple dictionnaire de module, partagé par toutes les requêtes du serveur :

    03h00 · l'automatisation « range mes mails » démarre       → canal = "fond"
    03h02 · Lohan pose une question dans le chat depuis son lit → canal = "web"
    03h03 · l'automatisation arrive sur GMAIL_SEND_EMAIL. Elle appelle le garde-fou
            SANS lui passer de canal (la boucle ReAct ne peut pas le transmettre), il
            lit donc la valeur globale : « web ». Le refus « fond » est contourné,
            l'envoi est ARMÉ sur le canal du chat.
    07h00 · Lohan tape « ok » pour tout autre chose. Le mail part.

Reproduit en trois lignes avant correction. C'est exactement ce qu'il redoutait dès le
début : « tu te rends compte s'il fait ça avec mes mails ! »

LA CORRECTION. Le canal suit le TRAVAIL, plus le processus :
  • sur la boucle asyncio, un contextvar — chaque requête est une tâche distincte, donc
    deux requêtes simultanées ne se marchent plus dessus ;
  • dans les threads d'exécution (les outils passent par run_in_executor, et un
    contextvar ne franchit pas cette frontière — piège déjà rencontré ailleurs ici),
    une variable locale au thread, posée au moment où le travail y est envoyé.
On lit d'abord le thread, puis la tâche. Par défaut « web » : le plus restreint des
canaux qui laisse encore Lohan confirmer lui-même.
"""
import contextvars
import threading

_TACHE = contextvars.ContextVar("canal_nova", default="")
_THREAD = threading.local()


def pose(canal: str) -> None:
    """Déclare le canal de la requête en cours (sur la boucle asyncio)."""
    _TACHE.set((canal or "web").strip() or "web")


def courant() -> str:
    """Le canal du travail en cours. Le thread prime : c'est lui qui exécute l'outil."""
    du_thread = getattr(_THREAD, "canal", "")
    if du_thread:
        return du_thread
    return _TACHE.get("") or "web"


def applique(canal: str, fn, *args, **kwargs):
    """Exécute `fn` dans CE thread en lui attachant le canal, puis le restitue.

    ⚠️ La restitution compte autant que la pose : les threads d'un pool sont réutilisés,
    et un canal laissé derrière soi contaminerait la requête suivante — c'est-à-dire
    exactement le défaut qu'on répare.
    """
    ancien = getattr(_THREAD, "canal", "")
    _THREAD.canal = (canal or "").strip()
    try:
        return fn(*args, **kwargs)
    finally:
        _THREAD.canal = ancien


class Registre(dict):
    """Compatible avec l'ancien `_CANAL["actuel"] = …`, mais qui pose VRAIMENT le canal.

    Tous les endroits qui écrivaient dans le dictionnaire continuent de fonctionner :
    l'écriture est simplement redirigée là où elle est fiable. On garde la valeur dans
    le dictionnaire pour les lectures d'affichage, jamais pour décider d'un envoi.
    """

    def __setitem__(self, cle, valeur):
        super().__setitem__(cle, valeur)
        if cle == "actuel":
            pose(str(valeur or "web"))

    def get(self, cle, defaut=None):
        if cle == "actuel":
            return courant()
        return super().get(cle, defaut)
