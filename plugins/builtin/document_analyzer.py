"""
Analyseur de documents — lit un PDF, un document texte, ou un dossier entier, et
répond à une question ou en fait un résumé/analyse.

Formats : .pdf (via pypdf), .txt .md .csv .json .log, et fichiers de code.
Rapide : découpe en morceaux, résume chaque morceau, puis synthétise (map-reduce)
→ quelques appels IA même pour un gros document.
"""
from pathlib import Path
from plugins.base import Plugin

_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".log", ".yaml", ".yml",
    ".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp", ".go",
    ".rs", ".rb", ".php", ".sql", ".sh", ".ini", ".cfg", ".xml",
}
_SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build"}
_CHUNK      = 7000     # caractères par morceau envoyé au LLM
_MAX_CHUNKS = 6        # plafond de morceaux (donc d'appels IA)


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # ancien nom, fallback
        except ImportError:
            return "__NO_PDF_LIB__"
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        return f"__PDF_ERR__{e}"


def _read_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        return "__NO_DOCX_LIB__"
    try:
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    except Exception as e:
        return f"__DOCX_ERR__{e}"


def _extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _read_pdf(path)
    if ext == ".docx":
        return _read_docx(path)
    if ext in _TEXT_EXT:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return f"__ERR__{e}"
    return ""


def _gather(root: Path) -> str:
    """Concatène le texte d'un fichier ou de tous les fichiers d'un dossier."""
    if root.is_file():
        return _extract_text(root)
    parts = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in _TEXT_EXT or p.suffix.lower() in (".pdf", ".docx"):
            t = _extract_text(p)
            if t and not t.startswith("__"):
                parts.append(f"===== {p.name} =====\n{t}")
        if sum(len(x) for x in parts) > _CHUNK * _MAX_CHUNKS:
            break
    return "\n\n".join(parts)


class DocumentAnalyzerPlugin(Plugin):
    name = "analyze_document"
    description = (
        "Lit et analyse un document (PDF, Word, texte) ou un dossier entier, et répond "
        "à une question ou en fait un résumé. Paramètres: path (fichier ou dossier), "
        "question (ce que tu veux savoir — optionnel, défaut = résumé)."
    )
    parameters = {
        "path":     {"type": "string", "description": "Chemin du fichier ou dossier", "required": True},
        "question": {"type": "string", "description": "Question / consigne (optionnel)", "required": False},
    }

    def run(self, path: str = "", question: str = "", **_) -> str:
        from llm.client import chat

        root = Path(str(path).strip().strip('"').strip("'")).expanduser()
        if not root.exists():
            return f"❌ Fichier/dossier introuvable : {path}"

        text = _gather(root)
        if text == "__NO_PDF_LIB__":
            return "❌ Lecture PDF indisponible. Installe la librairie :\n`pip install pypdf`"
        if text == "__NO_DOCX_LIB__":
            return "❌ Lecture Word indisponible. Installe :\n`pip install python-docx`"
        if not text or text.startswith("__"):
            return f"❌ Impossible d'extraire du texte de : {path} ({text[:80]})"

        question = (question or "").strip() or "Fais un résumé clair et structuré du contenu."

        # Découpe en morceaux
        chunks = [text[i:i + _CHUNK] for i in range(0, len(text), _CHUNK)][:_MAX_CHUNKS]
        truncated = len(text) > _CHUNK * _MAX_CHUNKS

        # 1 seul morceau → réponse directe
        if len(chunks) == 1:
            try:
                answer = chat([
                    {"role": "system", "content": "Tu es un analyste précis. Réponds en français, structuré."},
                    {"role": "user", "content": f"Document :\n\"\"\"\n{chunks[0]}\n\"\"\"\n\nConsigne : {question}"},
                ], temperature=0.2)
            except Exception as e:
                return f"❌ Erreur analyse : {e}"
            return f"## 📄 Analyse — {root.name}\n\n{answer}"

        # Plusieurs morceaux → map (résumé partiel) puis reduce (synthèse)
        partials = []
        for i, ch in enumerate(chunks, 1):
            try:
                p = chat([{"role": "user", "content": (
                    f"Extrait {i}/{len(chunks)} d'un document :\n\"\"\"\n{ch}\n\"\"\"\n\n"
                    f"En vue de répondre à : « {question} », note les éléments clés de CET extrait (bref)."
                )}], temperature=0.2)
                partials.append(f"[Extrait {i}] {p.strip()}")
            except Exception:
                pass

        if not partials:
            return "❌ Analyse impossible (le modèle n'a pas répondu)."

        try:
            final = chat([
                {"role": "system", "content": "Tu es un analyste précis. Réponds en français, structuré."},
                {"role": "user", "content": (
                    "Notes issues des différents extraits d'un document :\n\n"
                    + "\n\n".join(partials)
                    + f"\n\nEn te basant sur ces notes, réponds à la consigne : {question}"
                )},
            ], temperature=0.2)
        except Exception as e:
            return f"❌ Erreur synthèse : {e}"

        out = f"## 📄 Analyse — {root.name}\n\n{final}"
        if truncated:
            out += f"\n\n⚠️ Document très long : seuls les {_MAX_CHUNKS} premiers morceaux ont été lus."
        return out
