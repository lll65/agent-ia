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
    description = "Génère un VRAI script de vidéo virale sur-mesure (3 hooks, structure rétention timecodée, B-roll, CTA, titre+hashtags)."
    parameters = {
        "topic": {"type": "string", "description": "Sujet de la vidéo", "required": True},
        "style": {"type": "string", "description": "Style: éducatif | motivation | humour | tutoriel | storytelling", "required": False},
        "duration": {"type": "string", "description": "Durée cible: 30s | 45s | 60s | 90s", "required": False},
    }

    def run(self, topic: str, style: str = "éducatif", duration: str = "45s") -> str:
        # 1) Génération sur-mesure via LLM
        try:
            from llm.client import chat
            prompt = (
                f"Écris un script de vidéo courte virale (TikTok/Reels/Shorts) prêt à tourner.\n"
                f"Sujet : {topic}\nStyle : {style}\nDurée cible : {duration}\n\n"
                "Structure imposée :\n"
                "1. **3 variantes de HOOK** (0-3s) qui scotchent, numérotées\n"
                "2. **SCRIPT beat par beat** avec timecodes — texte dit + ce qui est montré à l'écran\n"
                "3. **B-roll / visuels** suggérés pour chaque segment\n"
                "4. **CALL TO ACTION** final percutant\n"
                "5. **TITRE + DESCRIPTION** optimisés + 8 hashtags pertinents\n\n"
                "Sois concret, rythmé, orienté rétention. Réponds en Markdown structuré, en français."
            )
            out = chat(
                [{"role": "system", "content": "Tu es un scénariste de contenu viral. Tu écris des scripts prêts à filmer, pas des plans génériques."},
                 {"role": "user", "content": prompt}],
                temperature=0.85,
            )
            if out and len(out.strip()) > 80:
                return out.strip()
        except Exception:
            pass

        # 2) Fallback statique si LLM indisponible
        return textwrap.dedent(f"""\
            === SCRIPT VIDÉO VIRALE ({style.upper()}) — {duration} ===
            Sujet: {topic}

            [HOOK 0-3s] — 3 variantes à tester
            1. "Personne ne te dit ça sur {topic}..."
            2. "J'ai testé {topic} pendant 30 jours, voici le résultat."
            3. "Arrête de faire ça avec {topic}."

            [DÉVELOPPEMENT]
            • Point clé 1 — explication simple
            • Point clé 2 — exemple concret / histoire courte
            • Twist / révélation inattendue

            [CALL TO ACTION]
            "Sauvegarde si ça t'a servi 💡 — Suis pour plus de {topic}"

            FORMAT: Vertical 9:16 | Sous-titres | Musique tendance en fond
        """)
