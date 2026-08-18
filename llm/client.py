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


def _providers_disponibles():
    """Chaîne de secours : le fournisseur préféré d'abord, puis TOUS les autres configurés.
    Avant, seuls 2 étaient essayés — si Cerebras passait en payant (402) et Groq atteignait sa
    limite, Nova devenait muette alors qu'une autre clé était peut-être disponible."""
    chaine = []

    def add(nom, cle, fn, modele):
        if cle and nom not in [c[0] for c in chaine]:
            chaine.append((nom, fn, modele))

    prefere = (config.LLM_PROVIDER or "").lower()
    tous = {
        "cerebras": (config.CEREBRAS_API_KEY, _cerebras_chat, config.CEREBRAS_MODEL),
        "groq":     (config.GROQ_API_KEY, _groq_chat, config.GROQ_MODEL),
        "gemini":   (getattr(config, "GEMINI_API_KEY", ""), _gemini_chat, config.GEMINI_MODEL),
        "xai":      (getattr(config, "XAI_API_KEY", ""), _xai_chat, getattr(config, "XAI_MODEL", "")),
    }
    if prefere in tous:                       # le préféré passe en tête
        add(prefere, *tous[prefere])
    for nom, (cle, fn, mod) in tous.items():
        add(nom, cle, fn, mod)
    return chaine


def _explique(nom: str, err: Exception) -> str:
    """Message clair pour l'utilisateur selon le type de panne."""
    t = str(err).lower()
    if "payment_required" in t or "402" in t or "billing" in t:
        return f"{nom} : offre gratuite épuisée (paiement demandé)"
    if _is_rate_limit(err):
        return f"{nom} : limite journalière atteinte"
    if "401" in t or "invalid api key" in t or "unauthorized" in t:
        return f"{nom} : clé invalide"
    if "not_found" in t or "404" in t:
        return f"{nom} : modèle indisponible"
    return f"{nom} : {str(err)[:70]}"


def chat(messages: list, temperature: float = 0.7, num_ctx: int = 4096) -> str:
    """Appel synchrone. Essaie chaque fournisseur configuré jusqu'à ce qu'un réponde."""
    chaine = _providers_disponibles()
    if not chaine:
        return _ollama_chat(messages, config.LLM_MODEL, temperature, num_ctx)

    soucis = []
    for nom, fn, modele in chaine:
        try:
            out = fn(messages, modele or config.LLM_MODEL, temperature)
            if out and out.strip():
                if soucis:
                    logger.warning(f"[LLM] bascule sur {nom} après : {' | '.join(soucis)}")
                return out
            soucis.append(f"{nom} : réponse vide")
        except Exception as e:
            soucis.append(_explique(nom, e))
            logger.warning(f"[LLM] {nom} indisponible → fournisseur suivant. ({str(e)[:90]})")

    # Dernier recours : Ollama local (souvent absent en ligne)
    try:
        return _ollama_chat(messages, config.LLM_MODEL, temperature, num_ctx)
    except Exception:
        pass
    raise RuntimeError(
        "Aucun modèle disponible pour le moment.\n• " + "\n• ".join(soucis) +
        "\n\nAjoute ou renouvelle une clé gratuite : console.groq.com (Groq) "
        "ou aistudio.google.com (Gemini), puis mets-la dans les variables Render.")


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

    # 1) Groq vision — auto-guérison : les noms de modèles changent souvent (404).
    #    On essaie le modèle configuré, des noms connus, puis ceux réellement accessibles au compte.
    if config.GROQ_API_KEY:
        candidates = []
        for m in (config.GROQ_VISION_MODEL,
                  "meta-llama/llama-4-scout-17b-16e-instruct",
                  "meta-llama/llama-4-maverick-17b-128e-instruct",
                  "llama-3.2-90b-vision-preview",
                  "llama-3.2-11b-vision-preview"):
            if m and m not in candidates:
                candidates.append(m)
        try:
            from groq import Groq
            client = Groq(api_key=config.GROQ_API_KEY)
            try:  # modèles réellement disponibles sur CE compte
                for mo in client.models.list().data:
                    mid = getattr(mo, "id", "") or ""
                    if mid and mid not in candidates and any(
                            k in mid.lower() for k in ("vision", "scout", "maverick", "llava")):
                        candidates.append(mid)
            except Exception:
                pass
            for m in candidates:
                try:
                    resp = client.chat.completions.create(
                        model=m,
                        messages=[{"role": "user", "content": [
                            {"type": "text", "text": question},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ]}],
                        temperature=temperature, max_tokens=2048,
                    )
                    out = resp.choices[0].message.content or ""
                    if out.strip():
                        return out
                except Exception as e:
                    txt = str(e).lower()
                    if any(k in txt for k in ("not_found", "does not exist", "404", "decommission")):
                        logger.warning(f"[vision] modèle Groq '{m}' indisponible, essai suivant…")
                        continue
                    errors.append(f"Groq({m}): {str(e)[:90]}")
                    break
            errors.append("Groq: aucun modèle vision accessible")
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
