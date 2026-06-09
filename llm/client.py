"""
Client LLM unifié — choisit automatiquement le provider selon le .env :
  GROQ_API_KEY      → Groq (gratuit, Llama 3.3 70B)
  XAI_API_KEY       → xAI Grok
  CEREBRAS_API_KEY  → Cerebras (gratuit, inference ultra-rapide, Llama 3.3 70B)
  GEMINI_API_KEY    → Google AI Studio (Gemini 2.5 Flash, gratuit, sans CB)
  sinon              → Ollama local

Fallback automatique : si Groq renvoie 429 (rate limit) et qu'une clé Cerebras
est configurée, la requête bascule automatiquement sur Cerebras (même format
OpenAI que Groq). Si les deux échouent, l'erreur est loggée clairement.
"""
import logging
from config import config

logger = logging.getLogger(__name__)


def _is_rate_limit(exc: Exception) -> bool:
    """Détecte un 429 / rate-limit, quel que soit le SDK."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text or "quota" in text


def chat(messages: list, temperature: float = 0.7, num_ctx: int = 4096) -> str:
    """Appel synchrone — utilisable depuis n'importe quel contexte.

    Fallback automatique Groq → Cerebras sur erreur 429.
    """
    provider = config.LLM_PROVIDER
    model = config.LLM_MODEL

    if provider == "groq":
        try:
            return _groq_chat(messages, model, temperature)
        except Exception as e:
            if _is_rate_limit(e) and config.CEREBRAS_API_KEY:
                logger.warning(
                    f"[LLM] Groq 429 (rate limit) — bascule automatique sur Cerebras "
                    f"({config.CEREBRAS_MODEL})."
                )
                try:
                    return _cerebras_chat(messages, config.CEREBRAS_MODEL, temperature)
                except Exception as e2:
                    logger.error(
                        f"[LLM] ÉCHEC des deux providers. Groq: {e} | Cerebras: {e2}"
                    )
                    raise RuntimeError(
                        f"Groq ET Cerebras ont échoué. Groq(429)={e} ; Cerebras={e2}"
                    ) from e2
            # Pas un 429 ou pas de fallback dispo → on remonte clairement
            logger.error(f"[LLM] Groq a échoué (pas de fallback applicable): {e}")
            raise

    if provider == "xai":
        return _xai_chat(messages, model, temperature)
    if provider == "cerebras":
        return _cerebras_chat(messages, model, temperature)
    if provider == "gemini":
        return _gemini_chat(messages, model, temperature)
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


def _cerebras_chat(messages: list, model: str, temperature: float) -> str:
    """
    Cerebras Inference — API compatible OpenAI (même format que Groq).
    base_url: https://api.cerebras.ai/v1 — modèle par défaut llama-3.3-70b.
    """
    from openai import OpenAI

    if not config.CEREBRAS_API_KEY:
        raise RuntimeError("CEREBRAS_API_KEY absente — impossible d'appeler Cerebras.")

    client = OpenAI(
        api_key=config.CEREBRAS_API_KEY,
        base_url="https://api.cerebras.ai/v1",
    )
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=4096,
    )
    return resp.choices[0].message.content


def _gemini_chat(messages: list, model: str, temperature: float) -> str:
    """
    Google AI Studio (Gemini) via l'API REST generativeLanguage.
    Pas de dépendance lourde : utilise requests (déjà présent).

    Convertit le format OpenAI (role/content) vers le format Gemini :
      - role 'system'    → systemInstruction
      - role 'assistant' → 'model'
      - role 'user'      → 'user'
    """
    import requests

    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY absente — impossible d'appeler Gemini.")

    system_parts: list[str] = []
    contents: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "") or ""
        if role == "system":
            system_parts.append(text)
            continue
        g_role = "model" if role == "assistant" else "user"
        contents.append({"role": g_role, "parts": [{"text": text}]})

    payload: dict = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 4096,
        },
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    resp = requests.post(
        url,
        params={"key": config.GEMINI_API_KEY},
        json=payload,
        timeout=120,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"Gemini HTTP {resp.status_code}: {resp.text[:300]}"
        )

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        # Réponse vide possible si bloquée par les filtres de sécurité
        feedback = data.get("promptFeedback", {})
        raise RuntimeError(f"Gemini: réponse vide (feedback={feedback}).")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Gemini: aucun texte dans la réponse.")
    return text


def _ollama_chat(messages: list, model: str, temperature: float, num_ctx: int) -> str:
    import ollama
    client = ollama.Client(timeout=120)
    resp = client.chat(
        model=model,
        messages=messages,
        options={"temperature": temperature, "num_ctx": num_ctx},
    )
    return resp["message"]["content"]
