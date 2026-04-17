"""
Outils disponibles pour l'agent.
Chaque outil est une fonction Python pure que l'agent peut appeler.
"""
import subprocess
import os
import json
import textwrap
from pathlib import Path
from config import config

AVAILABLE_TOOLS = {
    "search_web": "Recherche sur internet via DuckDuckGo. Paramètre: query (str)",
    "exec_python": "Exécute du code Python. Paramètre: code (str). ATTENTION: code isolé.",
    "write_file": "Écrit un fichier. Paramètres: path (str), content (str)",
    "read_file": "Lit un fichier. Paramètre: path (str)",
    "list_files": "Liste les fichiers d'un dossier. Paramètre: path (str)",
    "create_project": "Crée la structure d'un projet. Paramètres: name (str), type (str: python|web|api)",
    "create_video_script": "Génère un script pour une vidéo virale. Paramètre: topic (str)",
}


def search_web(query: str) -> str:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        if not results:
            return "Aucun résultat trouvé."
        out = []
        for r in results:
            out.append(f"**{r.get('title', '')}**\n{r.get('body', '')}\nURL: {r.get('href', '')}")
        return "\n\n".join(out)
    except Exception as e:
        return f"Erreur recherche: {e}"


def exec_python(code: str) -> str:
    try:
        result = subprocess.run(
            ["python3", "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=config.OUTPUT_DIR,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if err:
            return f"Sortie: {out}\nErreur: {err}" if out else f"Erreur: {err}"
        return out or "(aucune sortie)"
    except subprocess.TimeoutExpired:
        return "Timeout: le code a pris trop de temps (>15s)"
    except Exception as e:
        return f"Erreur exécution: {e}"


def write_file(path: str, content: str) -> str:
    try:
        full_path = Path(config.OUTPUT_DIR) / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return f"Fichier créé: {full_path}"
    except Exception as e:
        return f"Erreur écriture: {e}"


def read_file(path: str) -> str:
    try:
        full_path = Path(config.OUTPUT_DIR) / path
        if not full_path.exists():
            return f"Fichier introuvable: {path}"
        return full_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"Erreur lecture: {e}"


def list_files(path: str = ".") -> str:
    try:
        full_path = Path(config.OUTPUT_DIR) / path
        if not full_path.exists():
            return f"Dossier introuvable: {path}"
        files = list(full_path.rglob("*"))
        return "\n".join(str(f.relative_to(config.OUTPUT_DIR)) for f in files if f.is_file()) or "(vide)"
    except Exception as e:
        return f"Erreur: {e}"


def create_project(name: str, type: str = "python") -> str:
    base = Path(config.OUTPUT_DIR) / name
    try:
        if type == "python":
            (base / "src").mkdir(parents=True, exist_ok=True)
            (base / "tests").mkdir(exist_ok=True)
            (base / "src" / "__init__.py").write_text("")
            (base / "src" / "main.py").write_text(f'def main():\n    print("Projet {name}")\n\nif __name__ == "__main__":\n    main()\n')
            (base / "tests" / "test_main.py").write_text(f'from src.main import main\n\ndef test_main():\n    main()\n')
            (base / "requirements.txt").write_text("# Ajoute tes dépendances ici\n")
            (base / "README.md").write_text(f"# {name}\n\nDescription du projet.\n")
        elif type == "web":
            (base / "static" / "css").mkdir(parents=True, exist_ok=True)
            (base / "static" / "js").mkdir(exist_ok=True)
            (base / "index.html").write_text(textwrap.dedent(f"""\
                <!DOCTYPE html>
                <html lang="fr">
                <head><meta charset="UTF-8"><title>{name}</title>
                <link rel="stylesheet" href="static/css/style.css"></head>
                <body><h1>{name}</h1>
                <script src="static/js/app.js"></script></body>
                </html>
            """))
            (base / "static" / "css" / "style.css").write_text("body { font-family: sans-serif; margin: 2rem; }\n")
            (base / "static" / "js" / "app.js").write_text("console.log('Projet initialisé');\n")
        elif type == "api":
            (base / "api").mkdir(parents=True, exist_ok=True)
            (base / "main.py").write_text(textwrap.dedent("""\
                from fastapi import FastAPI
                app = FastAPI()

                @app.get("/")
                def root():
                    return {"message": "API opérationnelle"}
            """))
            (base / "requirements.txt").write_text("fastapi\nuvicorn\n")
        return f"Projet '{name}' ({type}) créé dans {base}"
    except Exception as e:
        return f"Erreur création projet: {e}"


def create_video_script(topic: str) -> str:
    script = textwrap.dedent(f"""\
        === SCRIPT VIDÉO VIRALE : {topic.upper()} ===

        [ACCROCHE - 0-3s]
        Question choc ou fait surprenant sur: {topic}

        [DÉVELOPPEMENT - 3-45s]
        Point 1: Explication principale
        Point 2: Exemple concret / histoire
        Point 3: Twist / révélation inattendue

        [CALL TO ACTION - 45-60s]
        "Sauvegarde cette vidéo si tu veux..."
        "Commente ce que tu en penses..."
        "Suis pour plus de contenu sur {topic}"

        FORMAT: Vertical 9:16 | Sous-titres obligatoires | Musique tendance
    """)
    return script


TOOL_FUNCTIONS = {
    "search_web": search_web,
    "exec_python": exec_python,
    "write_file": write_file,
    "read_file": read_file,
    "list_files": list_files,
    "create_project": create_project,
    "create_video_script": create_video_script,
}


def run_tool(tool_name: str, params: dict) -> str:
    fn = TOOL_FUNCTIONS.get(tool_name)
    if not fn:
        return f"Outil inconnu: {tool_name}. Outils disponibles: {list(TOOL_FUNCTIONS.keys())}"
    try:
        return fn(**params)
    except TypeError as e:
        return f"Paramètres incorrects pour {tool_name}: {e}"
