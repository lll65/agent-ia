import os

class Config:
    # LLM local via Ollama (tourne sur ta machine, 100% gratuit)
    OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

    # Serveur API
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", 8000))

    # Stockage
    DB_PATH = os.getenv("DB_PATH", "data/memory.db")
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")

    # Agent
    MAX_ITERATIONS = 10
    AGENTS_DIR = "data/agents"

config = Config()
