"""
Client LLM unifié — choisit automatiquement le provider selon le .env :
  GROQ_API_KEY  → Groq (gratuit, Llama 3.3 70B)
  XAI_API_KEY   → xAI Grok
  sinon          → Ollama local
"""
import logging
from config import config

logger = logging.getLogger(__name__)


def chat(messages: list, temperature: float = 0.7, num_ctx: int = 4096) -> str:
    """Appel synchrone — utilisable depuis n'importe quel contexte."""
    provider = config.LLM_PROVIDER
    model = config.LLM_MODEL

    if provider == "groq":
        return _groq_chat(messages, model, temperature)
    if provider == "xai":
        return _xai_chat(messages, model, temperature)
    return _ollama_chat(messages, model, temperature, num_ctx)


def _groq_chat(messages: list, model: str, temperature: float) -> str:
    from groq import Groq
    client = Groq(api_key=config.GROQ_API_KEY)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=4096,
    )
    return resp.choices[0].message.content


def _xai_chat(messages: list, model: str, temperature: float) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=config.XAI_API_KEY, base_url="https://api.x.ai/v1")
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=4096,
    )
    return resp.choices[0].message.content


def _ollama_chat(messages: list, model: str, temperature: float, num_ctx: int) -> str:
    import ollama
    client = ollama.Client(timeout=120)
    resp = client.chat(
        model=model,
        messages=messages,
        options={"temperature": temperature, "num_ctx": num_ctx},
    )
    return resp["message"]["content"]
