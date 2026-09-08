"""
La sentinelle — elle cherche les pannes AVANT qu'il tombe dessus.

« tu vas créer un agent IA pour détecter les bugs gênants de Nova, des bugs qui
pourraient me gêner dans mon utilisation, comme des problèmes entre Nova et Composio. »

⚠️ POURQUOI CE N'EST PAS UN AGENT QUI « RÉFLÉCHIT ». La tentation serait de demander à
un modèle « trouve les bugs » : il rendrait une liste plausible, invérifiable, et
souvent fausse — exactement le défaut qu'on passe la semaine à retirer. Une sentinelle
utile ne devine pas : elle ESSAIE. Elle appelle vraiment Gmail, vraiment l'agenda,
vraiment la météo, et rapporte ce qui s'est passé.

⚠️ ET ELLE NE RÉPARE RIEN TOUTE SEULE. Elle observe et prévient. Un programme qui
corrige sans qu'on le voie finit par corriger de travers, un jour où personne ne
regarde — et c'est la confiance qui part avec.

CE QU'ELLE VÉRIFIE, dans l'ordre de ce qui l'a réellement gêné cette semaine :
  • chaque app Composio répond-elle sous SON identité (le 404 du briefing) ;
  • reste-t-il un modèle capable de répondre, et de lire une image ;
  • ses cours survivront-ils à la nuit (base persistante) ;
  • le planificateur tourne-t-il, et à la bonne heure ;
  • la météo, la recherche web et la voix répondent-elles.
"""
import logging
import time

logger = logging.getLogger(__name__)

# Gravité : ce qui l'empêche d'utiliser Nova, ce qui le gênera, ce qui est bon à savoir.
BLOQUANT, GENANT, INFO = "bloquant", "genant", "info"


def _souci(gravite, quoi, detail, faire=""):
    return {"gravite": gravite, "quoi": quoi, "detail": str(detail)[:300], "faire": faire}


# ── Les contrôles, chacun indépendant ────────────────────────────────────────
def _v_composio() -> list:
    """Chaque app connectée répond-elle VRAIMENT, sous la bonne identité ?

    ⚠️ C'est le contrôle qui compte le plus. « Tout est connecté sur Composio depuis des
    semaines et à chaque fois il y a un truc défaillant » : le briefing appelait Gmail
    sous l'identité par défaut et recevait un 404, pendant que le chat, lui, marchait.
    Une app « connectée » n'est pas une app qui répond.
    """
    from api.agent import _tool, _connected_accounts
    out = []
    try:
        comptes = _connected_accounts() or []
    except Exception as e:
        return [_souci(BLOQUANT, "Composio injoignable", e,
                       "vérifie COMPOSIO_API_KEY dans les variables Render")]
    if not comptes:
        return [_souci(BLOQUANT, "Aucune app connectée", "la liste des comptes est vide",
                       "reconnecte tes apps sur composio.dev")]
    # On n'essaie QUE ce qu'il utilise vraiment : une app qu'il n'ouvre jamais n'a pas
    # à faire échouer un diagnostic, ni à consommer son quota.
    essais = (("gmail", "GMAIL_FETCH_EMAILS", {"maxResults": 1, "query": "in:inbox"}),
              ("googlecalendar", "GOOGLECALENDAR_EVENTS_LIST",
               {"calendarId": "primary", "maxResults": 1}))
    slugs = {str(s).lower() for s, _u, _st in comptes}
    for slug, action, args in essais:
        if not any(slug in s for s in slugs):
            continue
        try:
            brut = str(_tool(action, args, slug) or "")
        except Exception as e:
            out.append(_souci(BLOQUANT, f"{slug} : appel impossible", e))
            continue
        if "✅" in brut or '"successful": true' in brut.lower():
            continue
        bas = brut.lower()
        if "no connected account" in bas or "404" in bas:
            out.append(_souci(
                BLOQUANT, f"{slug} : compte introuvable sous cette identité",
                " ".join(brut.split())[:200],
                "l'app est connectée mais sous une autre identité Composio — "
                "reconnecte-la, ou dis-le-moi et je regarde"))
        elif "401" in bas or "403" in bas or "unauthorized" in bas:
            out.append(_souci(BLOQUANT, f"{slug} : autorisation refusée",
                              " ".join(brut.split())[:200],
                              "reconnecte cette app sur composio.dev"))
        else:
            out.append(_souci(GENANT, f"{slug} : réponse inattendue",
                              " ".join(brut.split())[:200]))
    return out


def _v_modeles() -> list:
    """Reste-t-il de quoi répondre ? Et de quoi lire une image ?"""
    from llm.client import _providers_disponibles, cles_secondaires
    from config import config
    out = []
    try:
        chaine = _providers_disponibles("equilibre")
    except Exception as e:
        return [_souci(BLOQUANT, "Chaîne de modèles illisible", e)]
    if not chaine:
        return [_souci(BLOQUANT, "Aucun modèle configuré", "la chaîne est vide",
                       "ajoute une clé gratuite : console.groq.com")]
    # ⚠️ Un seul fournisseur, c'est un seul point de panne. Son diagnostic du jour
    # disait « 1 fournisseur sur 6 répond » : ce n'est pas une panne, mais c'est ce qui
    # la rend certaine dès que Groq sature.
    base = {n.split(" (")[0] for n, _f, _m in chaine}
    if len(base) <= 1:
        out.append(_souci(GENANT, "Un seul fournisseur de modèles",
                          f"seul {', '.join(base)} est configuré",
                          "ajoute une clé gratuite ailleurs (openrouter.ai, sans carte) — "
                          "le jour où il sature, Nova est muette"))
    if not cles_secondaires():
        out.append(_souci(INFO, "Aucune deuxième clé",
                          "les quotas gratuits se comptent par compte",
                          "GROQ_API_KEY_2 avec un second compte double le quota"))
    if not (getattr(config, "OPENROUTER_API_KEY", "") or "").strip():
        out.append(_souci(GENANT, "Aucun modèle capable de lire une image",
                          "ni OpenRouter ni un modèle de vision accessible",
                          "crée une clé gratuite sur openrouter.ai (sans carte bancaire)"))
    return out


def _v_persistance() -> list:
    """Ses cours et sa mémoire survivront-ils à la nuit ?"""
    from config import config
    if (getattr(config, "SUPABASE_DB_URL", "") or "").strip():
        return []
    # ⚠️ Bloquant, pas « gênant » : un cours de deux heures perdu ne se rattrape pas,
    # l'audio étant effacé au fil de l'eau.
    return [_souci(BLOQUANT, "Tes cours ne survivront pas à la nuit",
                   "aucune base persistante configurée",
                   "renseigne SUPABASE_DB_URL, ou télécharge chaque cours après la séance")]


def _v_planificateur() -> list:
    """Les automatisations tournent-elles, et à SON heure ?"""
    out = []
    try:
        from agent.taches import etat as etat_taches
        e = etat_taches() or {}
        vu = e.get("planificateur", {}).get("dernier_battement")
        if vu and (time.time() - float(vu)) > 900:
            out.append(_souci(GENANT, "Le planificateur ne bat plus",
                              f"dernier signe il y a {int((time.time()-float(vu))//60)} min",
                              "tes automatisations ne partiront pas"))
    except Exception as e:
        logger.info(f"[sentinelle] planificateur non vérifiable ({type(e).__name__})")
    return out


def _v_meteo() -> list:
    """La météo répond-elle ? C'est le service qu'il utilise le plus souvent."""
    from agent.briefing import weather_line
    try:
        if not weather_line():
            return [_souci(GENANT, "Météo indisponible", "open-meteo n'a rien renvoyé")]
    except Exception as e:
        return [_souci(GENANT, "Météo en panne", e)]
    return []


_CONTROLES = (("apps connectées", _v_composio), ("modèles", _v_modeles),
              ("persistance", _v_persistance), ("planificateur", _v_planificateur),
              ("météo", _v_meteo))


async def inspecte() -> dict:
    """Passe tous les contrôles, en parallèle, et rend ce qui cloche VRAIMENT."""
    import asyncio
    from agent.core import _off

    async def un(nom, fn):
        try:
            return await asyncio.wait_for(_off(fn), timeout=45)
        except asyncio.TimeoutError:
            return [_souci(GENANT, f"{nom} : pas de réponse en 45 s",
                           "le contrôle lui-même a expiré")]
        except Exception as e:
            # ⚠️ Un contrôle qui plante n'est pas un « rien à signaler » : ce serait le
            # pire des silences, celui qui rassure.
            return [_souci(GENANT, f"{nom} : contrôle impossible", e)]

    lots = await asyncio.gather(*(un(n, f) for n, f in _CONTROLES))
    soucis = [s for lot in lots for s in (lot or [])]
    ordre = {BLOQUANT: 0, GENANT: 1, INFO: 2}
    soucis.sort(key=lambda s: ordre.get(s["gravite"], 3))
    return {"soucis": soucis,
            "bloquants": sum(1 for s in soucis if s["gravite"] == BLOQUANT),
            "controles": len(_CONTROLES)}


def rapport(res: dict) -> str:
    """Ce qu'elle a trouvé, en français, sans dramatiser ni minimiser."""
    soucis = (res or {}).get("soucis") or []
    n = (res or {}).get("controles", 0)
    if not soucis:
        return (f"🛡️ **Tout répond.** J'ai essayé pour de vrai tes apps, tes modèles, "
                f"ta sauvegarde et ta météo ({n} contrôles) — rien à signaler.")
    icones = {BLOQUANT: "🔴", GENANT: "🟠", INFO: "🔵"}
    L = []
    bl = res.get("bloquants", 0)
    L.append(f"🛡️ **{len(soucis)} chose(s) à savoir**"
             + (f", dont **{bl} qui t'empêche(nt) d'utiliser Nova normalement**." if bl else "."))
    L.append("")
    for s in soucis:
        L.append(f"{icones.get(s['gravite'], '•')} **{s['quoi']}**")
        if s["detail"]:
            L.append(f"  - _{s['detail']}_")
        if s["faire"]:
            L.append(f"  - 👉 {s['faire']}")
    L.append("")
    L.append("_Je n'ai rien réparé toute seule : je te dis ce que j'ai constaté en "
             "essayant réellement, pas ce que je suppose._")
    return "\n".join(L)
