from pathlib import Path
from plugins.base import Plugin
from config import config


def _safe_path(path: str) -> Path:
    """Empêche les path traversal — tout reste dans OUTPUT_DIR."""
    base = Path(config.OUTPUT_DIR).resolve()
    full = (base / path).resolve()
    if not str(full).startswith(str(base)):
        raise ValueError(f"Chemin interdit: {path}")
    return full


class WriteFilePlugin(Plugin):
    name = "write_file"
    description = "Écrit du contenu dans un fichier (dans le dossier output/). Paramètres: path (ou filename), content."
    parameters = {
        "path":     {"type": "string", "description": "Chemin relatif du fichier", "required": False},
        "filename": {"type": "string", "description": "Alias de path (accepté aussi)", "required": False},
        "content":  {"type": "string", "description": "Contenu à écrire", "required": True},
    }

    def validate(self, kwargs: dict) -> tuple[bool, str]:
        if "content" not in kwargs:
            return False, "Paramètres manquants: ['content']"
        if not kwargs.get("path") and not kwargs.get("filename"):
            return False, "Paramètres manquants: ['path'] — fournis le chemin du fichier"
        return True, ""

    def run(self, content: str, path: str = "", filename: str = "", **_) -> str:
        # Normalise l'alias 'filename' → 'path' (le LLM envoie souvent 'filename')
        path = path or filename
        if not path:
            return "❌ Paramètre 'path' manquant (chemin relatif du fichier à écrire)."
        full = _safe_path(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"✅ Fichier écrit: {full}"


class ReadFilePlugin(Plugin):
    name = "read_file"
    description = "Lit le contenu d'un fichier depuis output/."
    parameters = {"path": {"type": "string", "description": "Chemin relatif", "required": True}}

    def run(self, path: str) -> str:
        full = _safe_path(path)
        if not full.exists():
            return f"Fichier introuvable: {path}"
        content = full.read_text(encoding="utf-8")
        if len(content) > 8000:
            return content[:8000] + "\n... (tronqué)"
        return content


class ListFilesPlugin(Plugin):
    name = "list_files"
    description = "Liste les fichiers dans output/ (ou un sous-dossier)."
    parameters = {"path": {"type": "string", "description": "Sous-dossier (optionnel)", "required": False}}

    def run(self, path: str = ".") -> str:
        try:
            full = _safe_path(path)
            if not full.exists():
                return f"Dossier introuvable: {path}"
            files = [str(f.relative_to(config.OUTPUT_DIR)) for f in full.rglob("*") if f.is_file()]
            return "\n".join(files) if files else "(vide)"
        except Exception as e:
            return f"Erreur: {e}"
