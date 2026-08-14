import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    # API Keys cloud
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    XAI_API_KEY = os.getenv("XAI_API_KEY", "")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")     # Google AI Studio → ai.google.dev (gratuit, sans CB)
    CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "") # Cerebras → cloud.cerebras.ai (gratuit, inference ultra-rapide)
    HF_API_TOKEN = os.getenv("HF_API_TOKEN", "")
    REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")
    FAL_API_KEY = os.getenv("FAL_API_KEY", "")             # fal.ai → fal.ai/dashboard/keys
    LOCAL_SVD_URL = os.getenv("LOCAL_SVD_URL", "")         # Ex: http://192.168.1.100:9876

    # LLM local via Ollama (fallback si pas de clé cloud)
    OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "tinyllama")

    # Modèles cloud (surchargeable via .env)
    GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")  # analyse d'image
    # Recherche web fiable depuis un serveur (Render) — Tavily, ~1000/mois gratuit (tavily.com).
    # Optionnel : sans clé, on retombe sur DuckDuckGo (moins fiable en datacenter).
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
    # Passerelle universelle /ask : mot de passe pour parler à l'agent depuis n'importe quel
    # appareil (Siri, n8n, webhook…). VIDE = endpoint désactivé (501, aucune fuite).
    AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
    # Composio : connecteur universel (Gmail, Google Agenda, Slack, Notion… 1000+ apps).
    # Clé sur composio.dev (gratuit 20k actions/mois). COMPOSIO_USER_ID = ton "entity" (défaut 'default').
    COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "")
    COMPOSIO_USER_ID = os.getenv("COMPOSIO_USER_ID", "default")
    XAI_MODEL = os.getenv("XAI_MODEL", "grok-beta")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")

    # Fournisseur préféré : "cerebras" (1M tokens/jour gratuit = 10x Groq) ou "groq".
    LLM_PREFER = os.getenv("LLM_PREFER", "cerebras")

    @property
    def LLM_PROVIDER(self) -> str:
        # Cerebras d'abord si préféré et dispo (bien plus de tokens/jour gratuits)
        if self.LLM_PREFER == "cerebras" and self.CEREBRAS_API_KEY:
            return "cerebras"
        if self.GROQ_API_KEY:
            return "groq"
        if self.XAI_API_KEY:
            return "xai"
        if self.CEREBRAS_API_KEY:
            return "cerebras"
        if self.GEMINI_API_KEY:
            return "gemini"
        return "ollama"

    @property
    def LLM_MODEL(self) -> str:
        if self.LLM_PREFER == "cerebras" and self.CEREBRAS_API_KEY:
            return self.CEREBRAS_MODEL
        if self.GROQ_API_KEY:
            return self.GROQ_MODEL
        if self.XAI_API_KEY:
            return self.XAI_MODEL
        if self.CEREBRAS_API_KEY:
            return self.CEREBRAS_MODEL
        if self.GEMINI_API_KEY:
            return self.GEMINI_MODEL
        return self.OLLAMA_MODEL

    # Serveur API
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", 8000))

    # Stockage
    DB_PATH = os.getenv("DB_PATH", "data/memory.db")
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
    CHROMA_DIR = os.getenv("CHROMA_DIR", "data/chroma")
    # Mémoire persistante Supabase (Postgres + pgvector). Si défini, remplace
    # SQLite + ChromaDB → la mémoire survit aux redémarrages / mises en veille.
    # Récupère l'URL dans Supabase → Project Settings → Database → Connection string (URI).
    SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "")
    AGENTS_DIR = os.getenv("AGENTS_DIR", "data/agents")
    PLUGINS_DIR = os.getenv("PLUGINS_DIR", "data/plugins")

    # Agent
    MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "12"))
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
    SUMMARY_THRESHOLD = int(os.getenv("SUMMARY_THRESHOLD", "15"))  # résume après N messages

    # Bots (optionnels)
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # cible des alertes push (vide = tous les chats connus via /start)
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
    DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID") or "0")

    # PEA Watcher — surveillance autonome + alertes Telegram
    WATCHER_ENABLED   = os.getenv("WATCHER_ENABLED", "false").lower() == "true"
    WATCHER_INTERVAL  = int(os.getenv("WATCHER_INTERVAL", "1800"))   # secondes entre deux scans (défaut 30 min)
    WATCHER_RSI_LOW   = float(os.getenv("WATCHER_RSI_LOW", "32"))    # RSI ≤ seuil → survente / zone d'achat
    WATCHER_RSI_HIGH  = float(os.getenv("WATCHER_RSI_HIGH", "70"))   # RSI ≥ seuil → surachat / prendre profits
    WATCHER_MOVE_PCT  = float(os.getenv("WATCHER_MOVE_PCT", "5.0"))  # |variation journalière| ≥ seuil → alerte
    WATCHER_QUIET_HOURS = int(os.getenv("WATCHER_QUIET_HOURS", "20"))  # ré-alerte même règle après N h max 1×/jour

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
