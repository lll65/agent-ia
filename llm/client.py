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
        try:
            return _cerebras_chat(messages, model, temperature)
        except Exception as e:
            # Cerebras saturé/indispo → bascule sur Groq si dispo
            if config.GROQ_API_KEY:
                logger.warning(f"[LLM] Cerebras KO ({str(e)[:80]}) — bascule sur Groq.")
                try:
                    return _groq_chat(messages, config.GROQ_MODEL, temperature)
                except Exception as e2:
                    raise RuntimeError(f"Cerebras ET Groq ont échoué. Cerebras={e} ; Groq={e2}") from e2
            raise
    if provider == "gemini":
        return _gemini_chat(messages, model, temperature)
    return _ollama_chat(messages, model, temperature, num_ctx)


def chat_stream(messages: list, temperature: float = 0.6):
    """Générateur : produit la réponse token par token (vrai streaming).
    Compatible Groq/Cerebras (API OpenAI, stream=True). Repli non-stream sinon.
    Yield des morceaux de texte (str)."""
    from openai import OpenAI
    provider = config.LLM_PROVIDER
    model = config.LLM_MODEL
    try:
        if provider == "cerebras" and config.CEREBRAS_API_KEY:
            client = OpenAI(api_key=config.CEREBRAS_API_KEY, base_url="https://api.cerebras.ai/v1")
        elif provider == "groq" or config.GROQ_API_KEY:
            client = OpenAI(api_key=config.GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
            model = config.GROQ_MODEL if provider != "groq" else model
        else:
            # Provider non-stream (Gemini/Ollama) → on renvoie tout d'un coup
            yield chat(messages, temperature=temperature); return
        total = 0
        stream = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=4096, stream=True)
        for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content
            except Exception:
                delta = None
            if delta:
                total += 1
                yield delta
        try:
            from llm.usage import record
            record(total, provider=provider)  # approx (tokens ≈ chunks)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"[chat_stream] échec streaming ({str(e)[:80]}) → repli non-stream")
        try:
            yield chat(messages, temperature=temperature)
        except Exception as e2:
            yield f"❌ Erreur LLM : {str(e2)[:200]}"


def _groq_chat(messages: list, model: str, temperature: float) -> str:
    from groq import Groq
    client = Groq(api_key=config.GROQ_API_KEY)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=4096,
    )
    # Suivi de consommation (limite journalière gratuite)
    try:
        from llm.usage import record
        record(getattr(getattr(resp, "usage", None), "total_tokens", 0))
    except Exception:
        pass
    return resp.choices[0].message.content or ""


def _xai_chat(messages: list, model: str, temperature: float) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=config.XAI_API_KEY, base_url="https://api.x.ai/v1")
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=4096,
    )
    return resp.choices[0].message.content or ""


def chat_vision(image_path: str, prompt: str = "", temperature: float = 0.4) -> str:
    """
    Analyse une image. Essaie Groq (Llama 4 Scout) puis Gemini en repli :
    l'analyse de photos ne dépend donc plus d'un seul fournisseur.
    """
    import base64
    import mimetypes

    with open(image_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    question = prompt or "Décris cette image en détail, en français."
    errors = []

    # 1) Groq vision
    if config.GROQ_API_KEY:
        try:
            from groq import Groq
            client = Groq(api_key=config.GROQ_API_KEY)
            resp = client.chat.completions.create(
                model=config.GROQ_VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]}],
                temperature=temperature, max_tokens=2048,
            )
            out = resp.choices[0].message.content or ""
            if out.strip():
                return out
            errors.append("Groq: réponse vide")
        except Exception as e:
            errors.append(f"Groq: {str(e)[:120]}")

    # 2) Gemini vision (gratuit, sans carte bancaire)
    if getattr(config, "GEMINI_API_KEY", ""):
        try:
            import requests
            url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{config.GEMINI_MODEL}:generateContent")
            r = requests.post(url, params={"key": config.GEMINI_API_KEY}, timeout=90, json={
                "contents": [{"parts": [{"text": question},
                                        {"inline_data": {"mime_type": mime, "data": b64}}]}],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": 2048},
            })
            if r.status_code == 200:
                parts = ((r.json().get("candidates") or [{}])[0]
                         .get("content", {}).get("parts", []))
                out = "".join(p.get("text", "") for p in parts).strip()
                if out:
                    return out
            errors.append(f"Gemini: HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"Gemini: {str(e)[:120]}")

    raise RuntimeError(
        "Aucun modèle de vision disponible. Ajoute GROQ_API_KEY (gratuit sur console.groq.com) "
        "ou GEMINI_API_KEY (gratuit sur aistudio.google.com) dans les variables Render."
        + (f" Détails : {' | '.join(errors)}" if errors else ""))


def _cerebras_chat(messages: list, model: str, temperature: float) -> str:
    """
    Cerebras Inference — API compatible OpenAI (même format que Groq).
    Auto-guérison : si le modèle demandé n'existe pas (404), on réessaie avec
    des modèles Cerebras connus (le 8B est toujours dispo en gratuit).
    """
    from openai import OpenAI

    if not config.CEREBRAS_API_KEY:
        raise RuntimeError("CEREBRAS_API_KEY absente — impossible d'appeler Cerebras.")

    client = OpenAI(
        api_key=config.CEREBRAS_API_KEY,
        base_url="https://api.cerebras.ai/v1",
    )
    # Ordre d'essai : modèle configuré, noms connus, PUIS les modèles réellement
    # accessibles au compte (via /models) → évite les 404 'model_not_found'.
    candidates = []
    for m in (model, "llama-3.3-70b", "llama3.1-8b", "llama-3.1-8b",
              "llama3.1-70b", "llama-4-scout-17b-16e-instruct", "qwen-3-32b"):
        if m and m not in candidates:
            candidates.append(m)
    try:
        for mo in client.models.list().data:
            mid = getattr(mo, "id", None)
            if mid and mid not in candidates:
                candidates.append(mid)
    except Exception:
        pass

    last_err = None
    for m in candidates:
        try:
            resp = client.chat.completions.create(
                model=m,
                messages=messages,
                temperature=temperature,
                max_tokens=4096,
            )
            try:
                from llm.usage import record
                record(getattr(getattr(resp, "usage", None), "total_tokens", 0), provider="cerebras")
            except Exception:
                pass
            return resp.choices[0].message.content or ""
        except Exception as e:
            last_err = e
            txt = str(e).lower()
            # Modèle inexistant / non accessible → on tente le suivant
            if "not_found" in txt or "does not exist" in txt or "404" in txt:
                logger.warning(f"[LLM] Cerebras: modèle '{m}' indisponible, essai suivant…")
                continue
            raise
    raise RuntimeError(f"Cerebras: aucun modèle accessible ({last_err})")


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
