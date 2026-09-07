"""
Un cours qui s'ouvre dans LibreOffice — Writer ou Calc, au choix.

« il faudrait aussi une importation sur libre office, et où je choisis format Calc ou
normal ».

CE QU'ON PRODUIT. De vrais fichiers ODF, ceux que LibreOffice ouvre d'un double-clic :
  • .odt pour Writer  — le cours mis en forme, titres et tableaux compris ;
  • .ods pour Calc    — les TABLEAUX du cours, une feuille par tableau.

⚠️ POURQUOI PAS UN .DOCX OU UN .CSV. Un .csv perd la mise en forme et ne sait pas
porter plusieurs tableaux ; un .docx demanderait une dépendance de plus, et Nova tourne
sur l'offre gratuite de Render. L'ODF est le format NATIF de LibreOffice : c'est une
archive ZIP contenant du XML, on sait la fabriquer sans rien installer.

⚠️ CE QUE JE N'AI PAS PU VÉRIFIER. Je n'ai pas LibreOffice ici : je vérifie que
l'archive est valide, que chaque XML se parse et que la structure exigée par la norme
est là (mimetype non compressé en premier, manifeste, contenu). L'ouverture réelle dans
LibreOffice, non — dis-moi si un fichier est refusé et je corrige.
"""
import re
import zipfile
from xml.sax.saxutils import escape

_MIME_TEXTE = "application/vnd.oasis.opendocument.text"
_MIME_TABLEUR = "application/vnd.oasis.opendocument.spreadsheet"

_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"'
)

_STYLES = (
    '<office:automatic-styles>'
    '<style:style style:name="Gras" style:family="text">'
    '<style:text-properties fo:font-weight="bold"/></style:style>'
    '</office:automatic-styles>'
)


def _manifeste(mime: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest '
        'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.3">'
        f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{mime}"/>'
        '<manifest:file-entry manifest:full-path="content.xml" '
        'manifest:media-type="text/xml"/>'
        '</manifest:manifest>')


def _archive(chemin_ou_flux, mime: str, contenu: str) -> bytes:
    """L'archive ODF. Le « mimetype » DOIT être le premier fichier et NON compressé —
    c'est ce que la norme exige, et c'est à ça que LibreOffice reconnaît le format."""
    import io
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), mime, compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/manifest.xml", _manifeste(mime))
        z.writestr("content.xml", contenu)
    return tampon.getvalue()


# ── Lecture du markdown ──────────────────────────────────────────────────────
_TITRE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIGNE_TABLEAU = re.compile(r"^\s*\|(.+)\|\s*$")
_SEPARATEUR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _cellules(ligne: str) -> list:
    """Les cellules d'une ligne de tableau markdown, sans les barres."""
    corps = _LIGNE_TABLEAU.match(ligne).group(1)
    return [c.strip() for c in corps.split("|")]


def _sans_markdown(t: str) -> str:
    """Le texte, débarrassé de ce qui ne veut rien dire hors markdown."""
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[*_`~]", "", t)
    return t.strip()


def tableaux(md: str) -> list:
    """[(titre, [[cellules]])] — les tableaux du document, avec le titre qui précède."""
    out, titre, courant = [], "", []
    for ligne in (md or "").splitlines():
        m = _TITRE.match(ligne)
        if m:
            # ⚠️ On garde le dernier titre RENCONTRÉ, pas le premier : c'est celui qui
            # décrit le tableau qui suit. Sinon toutes les feuilles porteraient le nom
            # du cours.
            if courant:
                out.append((titre, courant))
                courant = []
            titre = _sans_markdown(m.group(2))
            continue
        if _LIGNE_TABLEAU.match(ligne):
            if _SEPARATEUR.match(ligne):
                continue          # la ligne de tirets ne porte aucune donnée
            courant.append([_sans_markdown(c) for c in _cellules(ligne)])
            continue
        if courant:
            out.append((titre, courant))
            courant = []
    if courant:
        out.append((titre, courant))
    return out


# ── Writer ───────────────────────────────────────────────────────────────────
def odt(md: str) -> bytes:
    """Le cours entier, mis en forme, pour LibreOffice Writer."""
    corps = []
    en_tableau = []

    def vide_tableau():
        if not en_tableau:
            return
        n = max(len(r) for r in en_tableau)
        nom = f"T{len(corps)}"
        cols = f'<table:table-column table:number-columns-repeated="{n}"/>'
        lignes = []
        for r in en_tableau:
            cells = "".join(
                "<table:table-cell office:value-type='string'>"
                f"<text:p>{escape(c)}</text:p></table:table-cell>"
                for c in (r + [""] * (n - len(r))))
            lignes.append(f"<table:table-row>{cells}</table:table-row>")
        corps.append(f'<table:table table:name="{nom}">{cols}{"".join(lignes)}</table:table>')
        en_tableau.clear()

    for ligne in (md or "").splitlines():
        if _LIGNE_TABLEAU.match(ligne):
            if not _SEPARATEUR.match(ligne):
                en_tableau.append([_sans_markdown(c) for c in _cellules(ligne)])
            continue
        vide_tableau()
        m = _TITRE.match(ligne)
        if m:
            niveau = min(6, len(m.group(1)))
            corps.append(f'<text:h text:outline-level="{niveau}">'
                         f"{escape(_sans_markdown(m.group(2)))}</text:h>")
            continue
        texte = _sans_markdown(ligne)
        # Une ligne vide sépare deux paragraphes : on la garde, elle se voit à l'écran.
        corps.append(f"<text:p>{escape(texte)}</text:p>")
    vide_tableau()

    contenu = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               f'<office:document-content {_NS} office:version="1.3">{_STYLES}'
               '<office:body><office:text>' + "".join(corps) +
               '</office:text></office:body></office:document-content>')
    return _archive(None, _MIME_TEXTE, contenu)


# ── Calc ─────────────────────────────────────────────────────────────────────
def _nom_feuille(titre: str, secours: str) -> str:
    """LibreOffice refuse : [ ] * ? : / \\ et un nom vide ou trop long."""
    n = re.sub(r"[\[\]\*\?:/\\']", " ", titre or "").strip()
    n = re.sub(r"\s+", " ", n)[:28]
    return n or secours


def ods(md: str) -> bytes:
    """Les TABLEAUX du cours, une feuille par tableau, pour LibreOffice Calc."""
    trouves = tableaux(md)
    feuilles = []
    vus = set()
    for i, (titre, grille) in enumerate(trouves, 1):
        nom = _nom_feuille(titre, f"Tableau {i}")
        # Deux tableaux sous le même titre : Calc refuse deux feuilles homonymes.
        base, k = nom, 2
        while nom.lower() in vus:
            nom = f"{base[:24]} ({k})"
            k += 1
        vus.add(nom.lower())
        n = max((len(r) for r in grille), default=1)
        lignes = []
        for r in grille:
            cells = "".join(
                "<table:table-cell office:value-type='string'>"
                f"<text:p>{escape(c)}</text:p></table:table-cell>"
                for c in (r + [""] * (n - len(r))))
            lignes.append(f"<table:table-row>{cells}</table:table-row>")
        feuilles.append(
            f'<table:table table:name="{escape(nom)}">'
            f'<table:table-column table:number-columns-repeated="{n}"/>'
            + "".join(lignes) + "</table:table>")

    if not feuilles:
        # ⚠️ Un classeur VIDE serait un fichier qui s'ouvre et ne dit rien : il croirait
        # que l'export a marché et que son cours n'a pas de tableaux. On l'écrit.
        feuilles.append(
            '<table:table table:name="Aucun tableau">'
            '<table:table-column/>'
            "<table:table-row><table:table-cell office:value-type='string'><text:p>"
            "Ce cours ne contient aucun tableau — le format Writer (.odt) garde tout "
            "le texte." "</text:p></table:table-cell></table:table-row></table:table>")

    contenu = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               f'<office:document-content {_NS} office:version="1.3">'
               '<office:body><office:spreadsheet>' + "".join(feuilles) +
               '</office:spreadsheet></office:body></office:document-content>')
    return _archive(None, _MIME_TABLEUR, contenu)
