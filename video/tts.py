"""
Text-to-Speech — utilise gTTS (Google TTS, HTTP simple, aucune clé API requise).
"""
import os
from pathlib import Path
from config import config


def text_to_speech(text: str, output_path: str, lang: str = "fr") -> str:
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang=lang, slow=False)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tts.save(str(path))
        return str(path)
    except ImportError:
        raise RuntimeError("gTTS non installé. Lance: pip install gTTS")
    except Exception as e:
        raise RuntimeError(f"Erreur TTS: {e}")
