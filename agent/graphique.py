"""
Des schémas tracés à partir des VRAIS chiffres.

Pourquoi pas une image générée par un modèle : parce qu'un modèle qui « dessine »
un graphique boursier invente la courbe. Elle serait jolie, plausible, et fausse —
exactement le défaut qu'on passe cet audit à éliminer, sauf qu'ici elle
illustrerait de l'argent. Ici, chaque point du dessin EST une donnée réelle.

Aucune dépendance : le SVG est du texte, on l'écrit à la main. Il part dans la
réponse sous forme d'image encodée (`![…](data:image/svg+xml;base64,…)`), que
l'interface sait déjà afficher. Pour Telegram, qui n'affiche pas ces images, on
rend la même information en caractères de bloc — lisible partout.
"""
import base64

# Huit hauteurs : de quoi dessiner une courbe reconnaissable en texte pur.
_BLOCS = "▁▂▃▄▅▆▇█"


def sparkline_texte(valeurs, largeur: int = 40) -> str:
    """La courbe en caractères de bloc — lisible sur Telegram, dans un mail, partout."""
    v = [float(x) for x in (valeurs or []) if x is not None]
    if len(v) < 2:
        return ""
    if len(v) > largeur:                       # on échantillonne sans déformer
        pas = len(v) / largeur
        v = [v[min(len(v) - 1, int(i * pas))] for i in range(largeur)]
    bas, haut = min(v), max(v)
    if haut == bas:
        return _BLOCS[3] * len(v)
    return "".join(_BLOCS[min(7, int((x - bas) / (haut - bas) * 7.999))] for x in v)


def courbe_svg(valeurs, titre: str = "", devise: str = "",
               largeur: int = 640, hauteur: int = 200) -> str:
    """Une courbe SVG lisible sur fond sombre, avec ses repères chiffrés."""
    v = [float(x) for x in (valeurs or []) if x is not None]
    if len(v) < 2:
        return ""
    bas, haut = min(v), max(v)
    if haut == bas:
        haut = bas + 1
    ml, mr, mt, mb = 8, 62, 26 if titre else 10, 18
    lx, ly = largeur - ml - mr, hauteur - mt - mb

    def pt(i, y):
        x = ml + (i / (len(v) - 1)) * lx
        yy = mt + ly - ((y - bas) / (haut - bas)) * ly
        return f"{x:.1f},{yy:.1f}"

    ligne = " ".join(pt(i, y) for i, y in enumerate(v))
    aire = f"{ml},{mt + ly} " + ligne + f" {ml + lx},{mt + ly}"
    # Vert si ça finit au-dessus du départ, rouge sinon : la couleur porte un FAIT.
    monte = v[-1] >= v[0]
    c = "#34d399" if monte else "#f87171"
    dern = pt(len(v) - 1, v[-1])

    def etiq(y, texte):
        yy = mt + ly - ((y - bas) / (haut - bas)) * ly
        return (f'<line x1="{ml}" y1="{yy:.1f}" x2="{ml + lx}" y2="{yy:.1f}" '
                f'stroke="#ffffff" stroke-opacity=".08"/>'
                f'<text x="{ml + lx + 6}" y="{yy + 4:.1f}" fill="#9ca3af" '
                f'font-size="11" font-family="system-ui,sans-serif">{texte}</text>')

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {largeur} {hauteur}" '
        f'width="{largeur}" height="{hauteur}" role="img">',
        f'<rect width="{largeur}" height="{hauteur}" rx="12" fill="#0f1020"/>',
    ]
    if titre:
        parts.append(f'<text x="{ml}" y="17" fill="#e5e7eb" font-size="12.5" '
                     f'font-family="system-ui,sans-serif">{_echappe(titre)}</text>')
    parts.append(etiq(haut, _fmt_nb(haut)))
    parts.append(etiq(bas, _fmt_nb(bas)))
    parts.append(f'<polygon points="{aire}" fill="{c}" fill-opacity=".12"/>')
    parts.append(f'<polyline points="{ligne}" fill="none" stroke="{c}" '
                 'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    parts.append(f'<circle cx="{dern.split(",")[0]}" cy="{dern.split(",")[1]}" r="3.5" fill="{c}"/>')
    parts.append(f'<text x="{ml + lx + 6}" y="{float(dern.split(",")[1]) + 4:.1f}" '
                 f'fill="{c}" font-size="11.5" font-weight="600" '
                 f'font-family="system-ui,sans-serif">'
                 f'{_fmt_nb(v[-1])} {_echappe(devise)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def barre_svg(paires, titre: str = "", largeur: int = 640) -> str:
    """Barres horizontales comparées — pour « ce volume face à son ordinaire »."""
    paires = [(str(n), float(v)) for n, v in (paires or []) if v is not None]
    if not paires:
        return ""
    maxi = max(v for _, v in paires) or 1
    h_barre, espace, mt = 26, 10, 26 if titre else 8
    hauteur = mt + len(paires) * (h_barre + espace) + 4
    ml, mr = 120, 60
    lx = largeur - ml - mr
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {largeur} {hauteur}" '
        f'width="{largeur}" height="{hauteur}" role="img">',
        f'<rect width="{largeur}" height="{hauteur}" rx="12" fill="#0f1020"/>',
    ]
    if titre:
        parts.append(f'<text x="12" y="17" fill="#e5e7eb" font-size="12.5" '
                     f'font-family="system-ui,sans-serif">{_echappe(titre)}</text>')
    for i, (nom, val) in enumerate(paires):
        y = mt + i * (h_barre + espace)
        w = max(2, (val / maxi) * lx)
        c = "#7c5cff" if i == 0 else "#4b5563"
        parts.append(f'<text x="{ml - 8}" y="{y + 17}" fill="#9ca3af" font-size="11.5" '
                     f'text-anchor="end" font-family="system-ui,sans-serif">{_echappe(nom)}</text>')
        parts.append(f'<rect x="{ml}" y="{y}" width="{w:.1f}" height="{h_barre}" rx="6" fill="{c}"/>')
        parts.append(f'<text x="{ml + w + 8:.1f}" y="{y + 17}" fill="#e5e7eb" font-size="11.5" '
                     f'font-family="system-ui,sans-serif">{_fmt(val)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _fmt_nb(v: float) -> str:
    """« 1 234.56 » — espace fine pour les milliers, jamais de virgule décimale
    ambiguë. (`"%,.2f" % v` n'existe pas en Python : c'est de la syntaxe C.)"""
    return f"{v:,.2f}".replace(",", " ")


def _fmt(v: float) -> str:
    if v >= 1e9:
        return f"{v/1e9:.2f} Md"
    if v >= 1e6:
        return f"{v/1e6:.1f} M"
    if v >= 1e3:
        return f"{v/1e3:.1f} k"
    return _fmt_nb(v)


def _echappe(t: str) -> str:
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def en_image(svg: str, alt: str = "schéma") -> str:
    """Le SVG sous forme d'image Markdown, prête à être affichée.

    Encodée en base64 : une image `data:` ne dépend d'aucun serveur, ne peut pas
    expirer, et l'interface sait déjà l'afficher (voir urlSure dans ui/nova.html,
    qui autorise justement `data:image/`).
    """
    if not svg:
        return ""
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"![{alt}](data:image/svg+xml;base64,{b64})"
