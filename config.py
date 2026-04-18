import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    # API Keys cloud
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    XAI_API_KEY = os.getenv("XAI_API_KEY", "")

    # LLM local via Ollama (fallback si pas de clé cloud)
    OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "tinyllama")

    @property
    def LLM_PROVIDER(self) -> str:
        if self.GROQ_API_KEY:
            return "groq"
        if self.XAI_API_KEY:
            return "xai"
        return "ollama"

    @property
    def LLM_MODEL(self) -> str:
        if self.GROQ_API_KEY:
            return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        if self.XAI_API_KEY:
            return os.getenv("XAI_MODEL", "grok-beta")
        return self.OLLAMA_MODEL

    # Serveur API
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", 8000))

    # Stockage
    DB_PATH = os.getenv("DB_PATH", "data/memory.db")
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
    CHROMA_DIR = os.getenv("CHROMA_DIR", "data/chroma")
    AGENTS_DIR = os.getenv("AGENTS_DIR", "data/agents")
    PLUGINS_DIR = os.getenv("PLUGINS_DIR", "data/plugins")

    # Agent
    MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "12"))
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
    SUMMARY_THRESHOLD = int(os.getenv("SUMMARY_THRESHOLD", "15"))  # résume après N messages

    # Bots (optionnels)
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
    DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID") or "0")

    # UI
    GRADIO_PORT = int(os.getenv("GRADIO_PORT", "7860"))
    GRADIO_SHARE = os.getenv("GRADIO_SHARE", "false").lower() == "true"

    # Vidéo
    VIDEO_RESOLUTION = tuple(map(int, os.getenv("VIDEO_RESOLUTION", "720x1280").split("x")))
    VIDEO_FPS = int(os.getenv("VIDEO_FPS", "24"))

    def ensure_dirs(self):
        for d in [self.OUTPUT_DIR, self.CHROMA_DIR, self.AGENTS_DIR, self.PLUGINS_DIR,
                  "data", f"{self.OUTPUT_DIR}/videos", f"{self.OUTPUT_DIR}/tmp"]:
            Path(d).mkdir(parents=True, exist_ok=True)


config = Config()
