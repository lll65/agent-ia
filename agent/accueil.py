"""
L'écran d'accueil — et RIEN qui ne soit mesuré.

« on garde ce qui est réaliste et fonctionnel ».

Le maquette proposait « Temps gagné : 2h34 », « Mails traités : 12 », « Ma réflexion en
direct : je croise plusieurs sources fiables ». Ces trois lignes ont un point commun :
personne ne les mesure. Ce sont des affirmations décoratives, et c'est exactement la
famille de défauts qu'on retire depuis deux jours — un chiffre qui a l'air d'une mesure,
une phrase qui affirme la qualité au lieu de montrer ce qui s'est passé.

CE MODULE NE REND QUE CE QU'UN OUTIL A RÉELLEMENT RENVOYÉ.

⚠️ LA RÈGLE QUI COMPTE : CHAQUE SECTION PORTE SON PROPRE ÉCHEC.
Une tuile « 0 mail » alors que Gmail a répondu 404, c'est un mensonge tranquille : il
voit zéro, il croit sa boîte vide, il ne va pas voir. Chaque bloc rend donc soit ses
données, soit `{"ok": false, "erreur": "…"}` — et l'interface AFFICHE l'erreur au lieu
d'un zéro. « Je n'ai pas pu regarder » et « il n'y a rien » ne se disent pas pareil.

⚠️ ET TOUT EST FACULTATIF. Une section en panne n'empêche jamais les autres de
s'afficher : elles sont récupérées en parallèle, chacune bornée dans le temps. Sur
l'offre gratuite de Render, un Composio lent ne doit pas retenir la météo.
"""
import logging

logger = logging.getLogger(__name__)

# Au-delà, on rend la main sans la section plutôt que de faire attendre l'écran.
DELAI = 12.0


def _erreur(e) -> dict:
    return {"ok": False, "erreur": f"{type(e).__name__}: {str(e)[:160]}"}


# ⚠️ ON EXIGE LA PREUVE DU SUCCÈS, PAS L'ABSENCE D'ÉCHEC CONNU.
# Première version : je cherchais « 404 », « [erreur] », « unauthorized »… dans la
# réponse. Sans Composio configuré, l'outil rend « ⚠️ Composio non configuré. Crée un
# compte gratuit… » — aucun de ces mots. La tuile affichait donc « 0 mail », c'est-à-dire
# « ta boîte est vide », alors que rien n'avait été regardé. Le défaut même que ce
# module devait empêcher, reproduit par ma façon de le chercher.
# Une liste de pannes connues sera toujours incomplète ; une marque de succès, non.
# `_tool` préfixe ses réussites d'un ✅ : c'est ÇA qu'on vérifie.
def _a_reussi(brut: str) -> bool:
    b = str(brut or "")
    return "✅" in b or '"successful": true' in b.lower()


# ── Les briques, chacune indépendante ────────────────────────────────────────
def _mails() -> dict:
    from api.agent import _tool
    from plugins.builtin.mails_tool import _extraire
    from agent.rapport_mail import trier, IMPORTANT, A_LIRE, IGNORER
    brut = _tool("GMAIL_FETCH_EMAILS", {"maxResults": 20, "query": "in:inbox is:unread"},
                 "gmail")
    b = str(brut or "")
    if not _a_reussi(b):
        return {"ok": False, "erreur": " ".join(b.split())[:200] or "aucune réponse"}
    mails = _extraire(b)
    # ⚠️ Gmail renvoie toujours une enveloppe. Si elle est grosse et qu'on n'a rien su
    # lire, ce n'est PAS une boîte vide : c'est notre lecture qui a raté.
    if not mails and len(b) > 400:
        return {"ok": False,
                "erreur": f"Gmail a répondu {len(b)} octets, aucun message reconnu"}
    tri = trier(mails)
    return {"ok": True, "total": len(mails),
            "important": len(tri.get(IMPORTANT) or []),
            "a_lire": len(tri.get(A_LIRE) or []),
            "ignores": len(tri.get(IGNORER) or []),
            "apercu": [{"de": (m.get("expediteur") or m.get("from") or "")[:60],
                        "objet": (m.get("objet") or m.get("subject") or "")[:90]}
                       for m in (tri.get(IMPORTANT) or [])[:3]]}


def _agenda() -> dict:
    import json
    import re
    from api.agent import _tool, _time_bounds
    tmin, tmax, _ = _time_bounds("aujourd'hui")
    brut = _tool("GOOGLECALENDAR_EVENTS_LIST",
                 {"calendarId": "primary", "timeMin": tmin, "timeMax": tmax,
                  "maxResults": 20, "singleEvents": True, "orderBy": "startTime"},
                 "googlecalendar")
    b = str(brut or "")
    if not _a_reussi(b):
        return {"ok": False, "erreur": " ".join(b.split())[:200] or "aucune réponse"}
    # On lit les objets d'événement où qu'ils soient dans l'enveloppe Composio.
    evts = []
    for m in re.finditer(r'\{[^{}]*"summary"\s*:', b):
        bout = b[m.start():]
        for fin in range(min(len(bout), 4000), 1, -1):
            try:
                o = json.loads(bout[:fin])
            except Exception:
                continue
            if isinstance(o, dict) and o.get("summary"):
                debut = ((o.get("start") or {}).get("dateTime")
                         or (o.get("start") or {}).get("date") or "")
                evts.append({"titre": str(o["summary"])[:80],
                             "debut": str(debut)[11:16] or "journée",
                             "lieu": str(o.get("location") or "")[:60]})
            break
    vus, propres = set(), []
    for e in evts:
        cle = (e["titre"], e["debut"])
        if cle not in vus:
            vus.add(cle)
            propres.append(e)
    propres.sort(key=lambda e: e["debut"])
    return {"ok": True, "total": len(propres), "evenements": propres[:8]}


def _meteo() -> dict:
    from agent.briefing import weather_line, ville_de_lohan
    ligne = weather_line()
    if not ligne:
        return {"ok": False, "erreur": "open-meteo n'a pas répondu"}
    return {"ok": True, "ligne": ligne, "ville": ville_de_lohan()}


def _taches() -> dict:
    """Ses automatisations : c'est ce que Nova fera pour lui, et c'est réel."""
    from agent.automations import list_all, prochaine_execution
    items = [a for a in (list_all() or []) if a.get("actif", True)]
    return {"ok": True, "total": len(items),
            "liste": [{"titre": str(a.get("titre") or "")[:60],
                       "icone": a.get("icon") or "⚡",
                       "quand": prochaine_execution(a)} for a in items[:6]]}


def _cours() -> dict:
    from agent import cours
    sessions = cours.lister() if hasattr(cours, "lister") else []
    return {"ok": True, "total": len(sessions),
            "dernier": (sessions[0].get("titre") if sessions else "")}


def _memoire() -> dict:
    from agent.profile import list_facts
    faits = list_facts() or []
    return {"ok": True, "total": len(faits)}


_BLOCS = (("mails", _mails), ("agenda", _agenda), ("meteo", _meteo),
          ("taches", _taches), ("cours", _cours), ("memoire", _memoire))


async def collecte() -> dict:
    """Toutes les sections, en parallèle, chacune bornée. Aucune n'en bloque une autre."""
    import asyncio
    from agent.core import _off

    async def une(nom, fn):
        try:
            return nom, await asyncio.wait_for(_off(fn), timeout=DELAI)
        except asyncio.TimeoutError:
            logger.info(f"[accueil] {nom} : délai dépassé")
            return nom, {"ok": False, "erreur": f"pas de réponse en {int(DELAI)} s"}
        except Exception as e:
            logger.info(f"[accueil] {nom} : {type(e).__name__}")
            return nom, _erreur(e)

    resultats = await asyncio.gather(*(une(n, f) for n, f in _BLOCS))
    out = dict(resultats)
    out["salutation"] = salutation()
    return out


def salutation() -> str:
    """Bonjour / bonsoir — d'après SON heure, pas celle du serveur.

    ⚠️ Le conteneur Render est en UTC. Sans ce détour, Nova lui souhaitait le bonsoir
    à 22 h alors qu'il est minuit chez lui, et bonjour à 6 h du matin pour 8 h.
    """
    from agent.horloge import maintenant
    h = maintenant().hour
    if h < 5:
        return "Bonne nuit"
    if h < 12:
        return "Bonjour"
    if h < 18:
        return "Bon après-midi"
    return "Bonsoir"
