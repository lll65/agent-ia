"""
Fiche d'une valeur — les faits qui permettent de VOIR VENIR un mouvement.

⚠️ Ce que cet outil ne fait PAS, et ne fera jamais : prédire une hausse ou une
baisse. Personne ne sait le faire, et une prédiction habillée en chiffres est
exactement le genre de « donnée fausse servie comme vraie » qu'on a passé cet
audit à traquer — sauf qu'ici elle porterait sur l'argent de quelqu'un.

Ce qu'il fait : rassembler les faits VÉRIFIABLES qui expliquent la plupart des
mouvements, en disant d'où vient chacun.

  • La DATE des prochains résultats. C'est de loin l'information la plus utile :
    un titre bouge plus le jour de ses résultats que pendant les trois semaines
    d'avant. Savoir « résultats dans 6 jours » change une décision.
  • Le VOLUME du jour rapporté à sa moyenne. Un volume trois fois supérieur à
    l'ordinaire signale que quelque chose se passe — souvent avant que la presse
    en parle.
  • La position dans l'année, les écarts aux moyennes mobiles, le RSI : le
    contexte, pas une prévision.
  • Les actualités datées, avec leur source.

Chaque chiffre absent est écrit « N/D » — jamais remplacé par une valeur
plausible.
"""
import logging

from plugins.base import Plugin

logger = logging.getLogger(__name__)


def _pct(a, b):
    try:
        return (float(a) / float(b) - 1) * 100
    except Exception:
        return None


def _n(v, fmt=".2f", suffixe=""):
    return "N/D" if v is None else (format(v, fmt) + suffixe)


def _plat(t):
    """Minuscules sans accents ni ponctuation — pour comparer deux noms de société."""
    import re as _re
    import unicodedata as _u
    t = _u.normalize("NFD", str(t or "").lower())
    t = "".join(c for c in t if _u.category(c) != "Mn")
    return _re.sub(r"[^a-z0-9]+", " ", t).strip()


# Ce qui figure dans presque toutes les raisons sociales et n'identifie donc personne.
# ⚠️ Les mots de SECTEUR en font partie. « After Energy » et « Energy Transfer LP »
# partagent « energy » : sans ça, le second passait pour le premier — et c'est
# exactement ce que la recherche web lui avait déjà servi.
_HABILLAGE = {"sa", "s a", "sas", "se", "plc", "ltd", "limited", "inc", "corp",
              "corporation", "nv", "ag", "spa", "group", "groupe", "holding",
              "holdings", "company", "co", "technologies", "technology", "the",
              "energy", "energie", "energies", "solutions", "systems", "systemes",
              "international", "france", "industries", "partners", "capital"}


def _meme_societe(nom_trouve: str, demande: str) -> bool:
    """Le titre trouvé est-il PLAUSIBLEMENT celui qu'il a nommé ?

    ⚠️ LE CAS RÉEL, ET IL EST GRAVE. Il demande « hafner énergie ». Le modèle propose le
    code HAFN ; Yahoo répond, la fiche s'affiche « 8,96 USD ». Sauf que HAFN, c'est
    Hafnia Limited — un armateur de pétroliers coté à New York. Haffner Energy, c'est
    ALHAF, à 0,67 € sur Euronext Growth. Deux sociétés sans aucun rapport, et Nova a
    rendu les deux chiffres dans la MÊME conversation sans jamais s'en apercevoir.

    Sur une question d'argent, un titre confondu avec un autre est le pire défaut
    possible : tout a l'air juste — le cours est réel, la source est réelle — mais il ne
    parle pas de la bonne entreprise.

    On ne cherche pas l'égalité : « Haffner Energy SA » et « haffner énergie » doivent
    s'accorder. Il suffit qu'un mot porteur soit commun, ou qu'un nom commence comme
    l'autre.
    """
    a, b = _plat(nom_trouve), _plat(demande)
    if not a or not b:
        return True                    # rien à comparer : on ne crie pas au loup
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    ma = [m for m in a.split() if m not in _HABILLAGE and len(m) > 2]
    mb = [m for m in b.split() if m not in _HABILLAGE and len(m) > 2]
    if not ma or not mb:
        return True
    from difflib import SequenceMatcher
    for x in mb:
        for y in ma:
            # ⚠️ PAS de comparaison par PRÉFIXE. « hafnia » et « hafner » partagent
            # « hafn » : le préfixe de quatre lettres faisait passer un armateur pour un
            # producteur d'hydrogène. On compare les mots ENTIERS, avec juste assez de
            # tolérance pour « haffner » / « hafner » (une lettre doublée) — 0,93 de
            # ressemblance — et pas assez pour « hafnia » / « hafner », qui plafonne
            # à 0,67.
            if x == y or SequenceMatcher(None, x, y).ratio() >= 0.85:
                return True
    return False


class FicheValeurPlugin(Plugin):
    name = "fiche_valeur"
    description = ("Fiche d'information sur une action : cours, volume inhabituel, "
                   "prochaine publication de résultats, position dans l'année, écarts "
                   "aux moyennes. Les faits qui permettent de voir venir un mouvement — "
                   "sans jamais le prédire.")
    parameters = {
        "ticker": {"type": "string", "description": "Code boursier (2CRSI → AL2SI.PA)",
                   "required": True},
        "societe": {"type": "string", "required": False, "description":
                    "Le nom de société tel que l'utilisateur l'a écrit. Toujours le "
                    "transmettre : il sert à vérifier que le code boursier désigne bien "
                    "CETTE entreprise."},
    }

    def run(self, ticker: str = "", societe: str = "", **_) -> str:
        tk = (ticker or "").strip().upper()
        if not tk:
            return "[ERREUR] Quel titre ?"
        try:
            return self._fiche(tk, societe)
        except Exception as e:
            logger.warning(f"[fiche_valeur] {tk} : {type(e).__name__}: {e}")
            return f"[ERREUR] Impossible de constituer la fiche de {tk} ({type(e).__name__})."

    def _fiche(self, tk: str, societe: str = "") -> str:
        from plugins.builtin.finance import _fetch_ticker_http, _rsi_liste, _volatilite_liste
        infos, closes, devise, source = {}, [], "", ""

        # Source principale : yfinance donne aussi le CALENDRIER (date des résultats),
        # qui est justement l'information la plus utile ici.
        try:
            import yfinance as yf
            t = yf.Ticker(tk)
            h = t.history(period="1y")
            if len(h):
                closes = [float(x) for x in h["Close"].dropna().tolist()]
                volumes = [float(x) for x in h["Volume"].dropna().tolist()]
                infos["volumes"] = volumes
                source = "Yahoo Finance"
            fi = {}
            try:
                fi = t.get_info() or {}
            except Exception:
                fi = getattr(t, "info", {}) or {}
            devise = fi.get("currency") or ""
            infos["nom"] = fi.get("longName") or fi.get("shortName") or tk
            infos["cap"] = fi.get("marketCap")
            infos["per"] = fi.get("trailingPE")
            infos["objectif"] = fi.get("targetMeanPrice")
            infos["avis"] = fi.get("recommendationKey")
            infos["analystes"] = fi.get("numberOfAnalystOpinions")
            try:
                cal = t.get_calendar()
                d = (cal or {}).get("Earnings Date") if isinstance(cal, dict) else None
                if d:
                    infos["resultats"] = str(d[0] if isinstance(d, (list, tuple)) else d)[:10]
            except Exception:
                pass
        except Exception:
            pass

        if not closes:                       # repli HTTP : moins riche, mais honnête
            d = _fetch_ticker_http(tk, "1y") or {}
            closes = d.get("closes") or []
            devise = devise or d.get("currency") or ""
            source = "Yahoo (accès direct)"
        if not closes:
            return (f"[ERREUR] Aucune donnée pour {tk}. Vérifie le code boursier — "
                    "une action de la Bourse de Paris finit en « .PA » (ex. AL2SI.PA).")

        prix = closes[-1]
        veille = closes[-2] if len(closes) > 1 else prix
        nom = infos.get("nom") or tk
        # ⚠️ AVANT TOUT CHIFFRE : ce code désigne-t-il bien la société qu'il a nommée ?
        # Un cours exact sur la mauvaise entreprise est plus dangereux qu'une erreur
        # visible — rien ne permet de s'en rendre compte à la lecture.
        if societe and not _meme_societe(nom, societe):
            return (f"[ERREUR] Le code **{tk}** correspond à **{nom}**"
                    + (f" (coté en {devise})" if devise else "")
                    + f", pas à « {societe} ». Je ne te donne PAS ces chiffres : ils sont "
                      "réels, mais ils ne parlent pas de la bonne entreprise. Redonne-moi "
                      "le code exact, ou le nom complet et je le cherche.")
        if societe and nom == tk:
            # Yahoo n'a pas rendu la raison sociale : on ne PEUT pas vérifier. On le dit,
            # au lieu de laisser croire que la concordance a été contrôlée.
            L = [f"## 📇 {tk}", "",
                 f"⚠️ **Je n'ai pas pu confirmer que {tk} est bien « {societe} »** — "
                 "Yahoo ne m'a pas rendu la raison sociale. Vérifie le code avant de "
                 "t'appuyer sur ces chiffres.", ""]
        else:
            L = [f"## 📇 {nom} ({tk})", ""]
        L.append(f"**{prix:,.2f} {devise}**  ·  {_pct(prix, veille):+.2f} % sur la séance"
                 if _pct(prix, veille) is not None else f"**{prix:,.2f} {devise}**")
        L.append("")

        # Un schéma tracé à partir des VRAIS points : chaque pixel est une donnée.
        # (Un modèle qui « dessinerait » cette courbe l'inventerait — jolie,
        # plausible, et fausse.)
        from agent.graphique import courbe_svg, sparkline_texte, en_image
        img = en_image(courbe_svg(closes[-252:], f"{infos.get('nom', tk)} — 1 an", devise),
                       f"Cours de {tk} sur 1 an")
        if img:
            L.append(img)
            # Même information en texte : Telegram et les mails n'affichent pas
            # les images encodées.
            L.append(f"`{sparkline_texte(closes[-252:])}`")
            L.append("")

        # ── 1. Ce qui est DATÉ et qui arrive ────────────────────────────────
        # Le rendez-vous connu à l'avance est ce qui déplace le plus un cours.
        L.append("### 📅 Ce qui arrive")
        if infos.get("resultats"):
            from datetime import date, datetime
            try:
                d = datetime.strptime(infos["resultats"], "%Y-%m-%d").date()
                jours = (d - date.today()).days
                quand = ("aujourd'hui" if jours == 0 else
                         f"dans {jours} jours" if 0 < jours < 400 else
                         f"il y a {-jours} jours")
                L.append(f"- **Prochains résultats : {infos['resultats']}** ({quand}). "
                         "C'est la date où le cours bouge le plus souvent.")
            except Exception:
                L.append(f"- Prochains résultats : {infos['resultats']}")
        else:
            L.append("- Date des prochains résultats : **N/D** (non publiée par la source).")

        # ── 2. Le signal qui précède souvent le mouvement ───────────────────
        L.append("")
        L.append("### 📊 Activité inhabituelle ?")
        vols = infos.get("volumes") or []
        if len(vols) >= 21:
            moy = sum(vols[-21:-1]) / 20
            ratio = (vols[-1] / moy) if moy else None
            if ratio is not None:
                signe = ("🔴 très inhabituel" if ratio >= 3 else
                         "🟠 inhabituel" if ratio >= 1.8 else
                         "🟡 un peu élevé" if ratio >= 1.3 else "⚪ normal")
                L.append(f"- Volume du jour : **{ratio:.1f}× la moyenne** des 20 séances — {signe}.")
                if ratio >= 1.8:
                    L.append("  - Un volume anormal veut dire qu'il se passe quelque chose. "
                             "Ça ne dit PAS dans quel sens.")
                from agent.graphique import barre_svg, en_image as _img
                b = _img(barre_svg([("aujourd'hui", vols[-1]), ("moyenne 20 j", moy)],
                                   "Titres échangés"), "Volume du jour face à sa moyenne")
                if b:
                    L.append("")
                    L.append(b)
        else:
            L.append("- Volume comparé à sa moyenne : **N/D**.")

        # ── 3. Le contexte, sans prophétie ─────────────────────────────────
        L.append("")
        L.append("### 📈 Où en est le titre")
        fenetre = closes[-252:]
        haut, bas = max(fenetre), min(fenetre)
        mois = max(1, round(len(fenetre) / 21))
        etiq = "sur 1 an" if len(fenetre) >= 200 else f"sur {mois} mois"
        if haut != bas:
            pos = (prix - bas) / (haut - bas) * 100
            L.append(f"- Fourchette {etiq} : **{bas:,.2f} – {haut:,.2f}** — "
                     f"il est à **{pos:.0f} %** de cette fourchette.")
        for n, nom in ((20, "20 séances"), (50, "50 séances"), (200, "200 séances")):
            if len(closes) >= n:
                m = sum(closes[-n:]) / n
                e = _pct(prix, m)
                L.append(f"- Moyenne {nom} : {m:,.2f} — le cours est "
                         f"**{e:+.1f} %** {'au-dessus' if e >= 0 else 'en dessous'}.")
        rsi = _rsi_liste(closes)
        vol = _volatilite_liste(closes[-60:])
        etat = ("N/D" if rsi is None else
                "🔥 zone de survente" if rsi < 30 else
                "❄️ zone de surachat" if rsi > 70 else "neutre")
        L.append(f"- RSI : **{_n(rsi, '.0f')}** ({etat})  ·  "
                 f"variation quotidienne moyenne : **{_n(vol, '.2f', ' %')}**")

        # ── 4. Ce que les analystes disent — en le nommant comme tel ────────
        if infos.get("objectif") or infos.get("cap") or infos.get("per"):
            L.append("")
            L.append("### 🏦 Repères")
            if infos.get("cap"):
                c = infos["cap"]
                cf = (f"{c/1e9:.2f} Md" if c >= 1e9 else f"{c/1e6:.0f} M")
                L.append(f"- Capitalisation : **{cf}{devise}**")
            if infos.get("per"):
                L.append(f"- PER : **{infos['per']:.1f}**")
            if infos.get("objectif"):
                e = _pct(infos["objectif"], prix)
                n = infos.get("analystes") or "?"
                L.append(f"- Objectif moyen des analystes : **{infos['objectif']:,.2f} {devise}** "
                         f"({e:+.0f} % vs le cours), d'après {n} analyste(s). "
                         "C'est un avis, pas une mesure.")

        # ── 5. « En clair » : sans ca, une liste de chiffres ne sert a rien ────
        # ⚠️ Lohan a 17 ans et apprend. Un tableau de RSI, de PER et de moyennes
        # mobiles est illisible sans une phrase qui dit ce que ca veut dire.
        L.append("")
        L.append("### 💡 En clair")
        clair = []
        if infos.get("resultats"):
            clair.append(f"La prochaine grosse date est le **{infos['resultats']}** "
                         "(publication des comptes). C'est là que le cours réagit le plus, "
                         "dans un sens ou dans l'autre.")
        if len(vols) >= 21:
            moy = sum(vols[-21:-1]) / 20
            r = (vols[-1] / moy) if moy else 0
            if r >= 1.8:
                clair.append(f"Il s'échange **{r:.1f} fois plus de titres que d'habitude** "
                             "aujourd'hui : quelque chose attire l'attention. Ça ne dit pas "
                             "si c'est bon ou mauvais — c'est un signal à creuser.")
            else:
                clair.append("Les échanges sont **au niveau habituel** : rien d'inhabituel "
                             "ne se passe sur le titre en ce moment.")
        if haut != bas:
            pos = (prix - bas) / (haut - bas) * 100
            si = ("proche de son plus haut" if pos > 80 else
                  "proche de son plus bas" if pos < 20 else "au milieu de sa fourchette")
            clair.append(f"Le cours est **{si}** {etiq}. Être haut ne veut pas dire "
                         "« trop cher », ni bas « bonne affaire » : ça situe, c'est tout.")
        if rsi is not None:
            if rsi > 70:
                clair.append("Le **RSI** (un indicateur qui mesure si un titre a beaucoup "
                             "monté d'un coup) est élevé : le titre a grimpé vite "
                             "récemment. Souvent suivi d'une pause — pas toujours.")
            elif rsi < 30:
                clair.append("Le **RSI** (un indicateur qui mesure si un titre a beaucoup "
                             "baissé d'un coup) est bas : il a chuté vite récemment.")
        if infos.get("objectif"):
            clair.append("L'**objectif des analystes** est une moyenne d'avis de "
                         "professionnels. Ils se trompent souvent : à lire comme une "
                         "opinion, pas comme une prévision.")
        L.extend(f"- {c}" for c in clair)

        L.append("")
        L.append(f"_Source des cours : {source}. "
                 "Ces chiffres décrivent le passé et le présent — **aucun ne prédit "
                 "l'avenir**. Ils servent à savoir quoi surveiller, pas quoi acheter._")
        return "\n".join(L)
