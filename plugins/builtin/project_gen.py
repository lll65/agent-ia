import textwrap
from pathlib import Path
from plugins.base import Plugin
from config import config


class CreateProjectPlugin(Plugin):
    name = "create_project"
    description = "Crée la structure complète d'un projet (python, web ou api)."
    parameters = {
        "name": {"type": "string", "description": "Nom du projet", "required": True},
        "type": {"type": "string", "description": "Type: python | web | api", "required": False},
        "description": {"type": "string", "description": "Description courte", "required": False},
    }

    def run(self, name: str, type: str = "python", description: str = "") -> str:
        base = Path(config.OUTPUT_DIR) / name
        if type == "python":
            (base / "src").mkdir(parents=True, exist_ok=True)
            (base / "tests").mkdir(exist_ok=True)
            (base / "src" / "__init__.py").write_text("")
            (base / "src" / "main.py").write_text(
                f'"""{ description or name }"""\n\ndef main():\n    print("Projet {name} démarré")\n\nif __name__ == "__main__":\n    main()\n'
            )
            (base / "tests" / "test_main.py").write_text(
                f'from src.main import main\n\ndef test_smoke():\n    main()\n'
            )
            (base / "requirements.txt").write_text("# Dépendances\n")
            (base / ".gitignore").write_text("__pycache__/\n*.pyc\n.env\n")
        elif type == "web":
            (base / "static" / "css").mkdir(parents=True, exist_ok=True)
            (base / "static" / "js").mkdir(exist_ok=True)
            (base / "index.html").write_text(textwrap.dedent(f"""\
                <!DOCTYPE html>
                <html lang="fr">
                <head>
                  <meta charset="UTF-8">
                  <meta name="viewport" content="width=device-width, initial-scale=1">
                  <title>{name}</title>
                  <link rel="stylesheet" href="static/css/style.css">
                </head>
                <body>
                  <main><h1>{name}</h1><p>{description}</p></main>
                  <script src="static/js/app.js"></script>
                </body>
                </html>
            """))
            (base / "static" / "css" / "style.css").write_text(
                "*, *::before, *::after { box-sizing: border-box; }\nbody { font-family: system-ui, sans-serif; margin: 2rem; }\n"
            )
            (base / "static" / "js" / "app.js").write_text("console.log('Prêt.');\n")
        elif type == "api":
            (base / "routers").mkdir(parents=True, exist_ok=True)
            (base / "main.py").write_text(textwrap.dedent(f"""\
                from fastapi import FastAPI
                from routers import items

                app = FastAPI(title="{name}", description="{description}")
                app.include_router(items.router, prefix="/items")

                @app.get("/")
                def root():
                    return {{"status": "ok", "service": "{name}"}}
            """))
            (base / "routers" / "__init__.py").write_text("")
            (base / "routers" / "items.py").write_text(textwrap.dedent("""\
                from fastapi import APIRouter
                router = APIRouter(tags=["items"])

                @router.get("/")
                def list_items():
                    return []
            """))
            (base / "requirements.txt").write_text("fastapi\nuvicorn\n")
        else:
            return f"Type inconnu: {type}. Valides: python, web, api"

        readme = f"# {name}\n\n{description or 'Projet créé par ton Agent IA.'}\n"
        (base / "README.md").write_text(readme)
        return f"Projet '{name}' ({type}) créé dans {base}"


class VideoScriptPlugin(Plugin):
    name = "create_video_script"
    description = "Génère un script de vidéo virale (slides percutantes)."
    parameters = {
        "topic": {"type": "string", "description": "Sujet de la vidéo", "required": True},
        "style": {"type": "string", "description": "Style: éducatif | motivation | humour | tutoriel", "required": False},
    }

    def run(self, topic: str, style: str = "éducatif") -> str:
        return textwrap.dedent(f"""\
            === SCRIPT VIDÉO VIRALE ({style.upper()}) ===
            Sujet: {topic}

            [SLIDE 1 — ACCROCHE 0-3s]
            Fait surprenant ou question choc sur: {topic}

            [SLIDE 2-4 — DÉVELOPPEMENT]
            • Point clé 1 — explication simple
            • Point clé 2 — exemple concret / histoire courte
            • Twist / révélation inattendue

            [SLIDE 5 — CALL TO ACTION]
            "Sauvegarde si tu veux retenir ça 💡"
            "Commente ton avis 👇"
            "Suis pour plus de {topic}"

            FORMAT: Vertical 9:16 | Sous-titres | Musique tendance en fond
        """)
