"""
Le réveil externe fait-il VRAIMENT son travail ?

⚠️ Sur l'offre gratuite de Render, l'instance s'endort après ~15 minutes sans
requête entrante. Une tâche de fond qui tourne — une automatisation, une synthèse
de cours — ne compte PAS comme activité : seul le trafic entrant réveille. C'est
pourquoi un cron externe doit appeler /health régulièrement, y compris pendant que
Nova travaille.

Mais « j'ai branché le cron » n'est pas une preuve. Un cron peut viser la mauvaise
URL, ou avoir été désactivé automatiquement par le service après des échecs
répétés (cron-job.org le fait) — ce qui arrive typiquement quand /health n'existait
pas encore et renvoyait 404. Ici on compte les passages réels : soit ils arrivent,
soit non, et le diagnostic le dit sans supposer.
"""
import time
from collections import deque

# Démarrage de CE processus. S'il ne cesse de se réinitialiser, l'instance dort.
DEMARRAGE = time.time()
_PASSAGES = deque(maxlen=400)      # horodatages des appels à /health


def note_passage() -> None:
    _PASSAGES.append(time.time())


def etat() -> dict:
    """Le réveil tient-il l'instance éveillée ? Réponse chiffrée, pas une supposition."""
    maintenant = time.time()
    depuis_demarrage = maintenant - DEMARRAGE
    recents = [t for t in _PASSAGES if maintenant - t < 3600]
    d = {
        "en_ligne_depuis_min": round(depuis_demarrage / 60, 1),
        "passages_derniere_heure": len(recents),
        "dernier_passage_il_y_a_s": round(maintenant - _PASSAGES[-1]) if _PASSAGES else None,
    }
    # Intervalle réellement observé — c'est lui qui dit si le rythme est bon.
    if len(recents) >= 2:
        ecarts = [round(b - a) for a, b in zip(recents, recents[1:])]
        d["intervalle_moyen_min"] = round(sum(ecarts) / len(ecarts) / 60, 1)
        d["plus_grand_trou_min"] = round(max(ecarts) / 60, 1)

    if not _PASSAGES and depuis_demarrage < 660:
        # ⚠️ Le compteur vit DANS le processus : un redémarrage le remet à zéro. Accuser
        # le cron dix secondes après un démarrage, c'est accuser à tort — il n'a pas
        # encore eu l'occasion de tirer. On attend un intervalle complet (11 min).
        d["resume"] = (f"⏳ L'instance vient de démarrer (il y a "
                       f"{round(depuis_demarrage / 60)} min) : le réveil externe n'a pas "
                       "encore eu l'occasion de passer. Reviens dans une dizaine de "
                       "minutes pour savoir s'il tire vraiment.")
    elif not _PASSAGES:
        d["resume"] = ("❌ AUCUN appel à /health depuis le démarrage : le réveil externe "
                       "ne tire pas. Render endormira l'instance et les automatisations "
                       "ne partiront pas.")
        # ⚠️ Le message donnait « https://TON-APP.onrender.com/health » en exemple.
        # Lohan l'a recopié TEL QUEL dans cron-job.org : le job appelait donc une
        # adresse qui n'existe pas, échouait à chaque fois, et le service a fini par
        # le désactiver tout seul. Un exemple qui ressemble à une vraie adresse SERA
        # recopié tel quel — on décrit donc l'adresse au lieu de l'écrire.
        d["solution"] = (
            "Sur cron-job.org, vérifie TROIS choses. "
            "① L'ADRESSE : ce doit être exactement celle que tu tapes pour ouvrir Nova, "
            "suivie de /health. Si tu y lis « ton-app » ou « TON-APP », c'est un exemple "
            "recopié tel quel — remplace-le par le vrai nom de ton service Render. "
            "② L'ÉTAT : si « Exécution suivante » affiche « Inactif », le job a été "
            "désactivé automatiquement après des échecs — réactive-le. "
            "③ L'INTERVALLE : 10 minutes maximum.")
    elif d["dernier_passage_il_y_a_s"] > 900:
        d["resume"] = (f"⚠️ Dernier appel à /health il y a "
                       f"{round(d['dernier_passage_il_y_a_s'] / 60)} min : c'est trop long, "
                       "Render s'endort au-delà d'un quart d'heure sans trafic.")
        d["solution"] = "Règle le cron sur 10 minutes maximum."
    elif d.get("plus_grand_trou_min", 0) > 14:
        d["resume"] = (f"⚠️ Le réveil tire, mais il a laissé un trou de "
                       f"{d['plus_grand_trou_min']} min — assez pour que Render s'endorme.")
        d["solution"] = "Descends l'intervalle à 5 minutes pour garder une marge."
    elif depuis_demarrage < 600 and len(recents) > 3:
        # L'instance vient de redémarrer alors que le cron tirait : elle a bien dormi.
        d["resume"] = ("⚠️ Le réveil tire correctement, mais l'instance a redémarré il y a "
                       f"{round(depuis_demarrage / 60)} min : elle s'est quand même "
                       "endormie (ou Render l'a redéployée).")
        d["solution"] = ("Si ça se répète, c'est le quota gratuit de Render qui est "
                         "atteint (750 h/mois — juste assez pour UN service allumé en "
                         "permanence). Mets en veille tout autre service du même compte.")
    else:
        d["resume"] = (f"✅ Le réveil tire toutes les {d.get('intervalle_moyen_min', '?')} min "
                       f"et l'instance est en ligne depuis {round(depuis_demarrage / 60)} min.")
    return d
