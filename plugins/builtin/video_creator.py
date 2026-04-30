from plugins.base import Plugin


class CreateVideoPlugin(Plugin):
    name = "create_viral_video"
    description = "Génère une vraie vidéo virale (MP4) à partir d'un sujet ou d'un lien produit."
    parameters = {
        "topic": {"type": "string", "description": "Sujet ou URL produit", "required": True},
        "style": {"type": "string", "description": "Style: viral, éducatif, motivation...", "required": False},
        "lang": {"type": "string", "description": "Langue: fr/en/es", "required": False},
        "n_slides": {"type": "integer", "description": "Nombre de scènes (3-10)", "required": False},
        "theme": {"type": "string", "description": "Thème visuel", "required": False},
        "add_audio": {"type": "boolean", "description": "Activer voix TTS", "required": False},
    }

    def run(self, topic: str, style: str = "viral", lang: str = "fr",
            n_slides: int = 6, theme: str = "dark", add_audio: bool = True) -> str:
        import asyncio
        from pathlib import Path
        from video.pipeline import create_pro_video

        n_slides = max(3, min(int(n_slides), 10))
        result = asyncio.run(create_pro_video(
            topic=topic,
            style=style,
            lang=lang,
            n_slides=n_slides,
            theme=theme,
            add_audio=bool(add_audio),
        ))
        vp = result.get("video_path")
        title = result.get("title", topic)
        if vp and Path(vp).exists():
            return f"✅ Vidéo générée: {vp}\nTitre: {title}\nTu peux la télécharger dans l'onglet Vidéo."
        return f"⚠️ Vidéo non générée correctement. Résultat brut: {result}"
