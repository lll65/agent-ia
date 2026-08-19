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


_MODELES_OK = {}   # fournisseur -> modèle qui a réellement fonctionné (mémorisé)


# ── ROUTAGE PAR TÂCHE ─────────────────────────────────────────────────────────
# Trois niveaux : « rapide » (discuter, mise en forme), « equilibre » (par défaut),
# « puissant » (code, analyse longue, raisonnement).
# Modèle choisi par fournisseur ET par niveau. Si un modèle n'existe plus,
# l'auto-guérison de chaque fournisseur prend le relais : le routage n'échoue jamais.
MODELES = {
    "nvidia": {"rapide": "meta/llama-3.1-8b-instruct",
               "equilibre": "meta/llama-3.3-70b-instruct",
               "puissant": "meta/llama-3.1-405b-instruct"},
    "groq": {"rapide": "llama-3.1-8b-instant",
             "equilibre": "llama-3.3-70b-versatile",
             "puissant": "llama-3.3-70b-versatile"},
    "gemini": {"rapide": "gemini-2.0-flash",
               "equilibre": "gemini-2.0-flash",
               "puissant": "gemini-2.5-pro"},
    "openrouter": {"rapide": "meta-llama/llama-3.2-3b-instruct:free",
                   "equilibre": "meta-llama/llama-3.3-70b-instruct:free",
                   "puissant": "deepseek/deepseek-r1:free"},
    "cerebras": {"rapide": "llama3.1-8b", "equilibre": "llama-3.3-70b", "puissant": "llama-3.3-70b"},
}

# Ordre des fournisseurs SELON le niveau (choix expliqué à l'utilisateur) :
# — rapide    : Groq d'abord, c'est le plus véloce
# — équilibré : NVIDIA d'abord, quota le plus généreux
# — puissant  : NVIDIA puis Gemini, ce sont eux qui ont les gros modèles
ORDRE = {
    "rapide":    ("groq", "nvidia", "gemini", "openrouter", "cerebras"),
    "equilibre": ("nvidia", "groq", "gemini", "openrouter", "cerebras"),
    "puissant":  ("nvidia", "gemini", "openrouter", "groq", "cerebras"),
}

# Qui a répondu en dernier (par contexte async → sûr même avec plusieurs requêtes)
import contextvars
DERNIER = contextvars.ContextVar("dernier_llm", default="")


def _providers_disponibles(niveau: str = "equilibre"):
    """Chaîne de secours : le fournisseur préféré d'abord, puis TOUS les autres configurés.
    Avant, seuls 2 étaient essayés — si Cerebras passait en payant (402) et Groq atteignait sa
    limite, Nova devenait muette alors qu'une autre clé était peut-être disponible."""
    chaine = []
    tous = {
        "nvidia":     (getattr(config, "NVIDIA_API_KEY", ""), _nvidia_chat),
        "groq":       (config.GROQ_API_KEY, _groq_chat),
        "gemini":     (getattr(config, "GEMINI_API_KEY", ""), _gemini_chat),
        "openrouter": (getattr(config, "OPENROUTER_API_KEY", ""), _openrouter_chat),
        "cerebras":   (config.CEREBRAS_API_KEY, _cerebras_chat),
        "xai":        (getattr(config, "XAI_API_KEY", ""), _xai_chat),
    }

    def add(nom):
        cle_fn = tous.get(nom)
        if not cle_fn or not cle_fn[0] or nom in [c[0] for c in chaine]:
            return
        modele = MODELES.get(nom, {}).get(niveau) or _MODELES_OK.get(nom) or config.LLM_MODEL
        chaine.append((nom, cle_fn[1], modele))

    # 1) Un fournisseur explicitement imposé par l'utilisateur passe toujours en tête
    prefere = (config.LLM_PROVIDER or "").lower()
    if prefere in tous:
        add(prefere)
    # 2) Puis l'ordre adapté au niveau de la tâche
    for nom in ORDRE.get(niveau, ORDRE["equilibre"]):
        add(nom)
    # 3) Enfin tout le reste (rien ne doit être oublié)
    for nom in tous:
        add(nom)
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


def chat(messages: list, temperature: float = 0.7, num_ctx: int = 4096,
         niveau: str = "equilibre") -> str:
    """Appel synchrone. Choisit le modèle adapté au niveau de la tâche, puis essaie
    chaque fournisseur jusqu'à ce qu'un réponde (le routage ne peut donc pas faire échouer)."""
    chaine = _providers_disponibles(niveau)
    if not chaine:
        return _ollama_chat(messages, config.LLM_MODEL, temperature, num_ctx)

    soucis = []
    for nom, fn, modele in chaine:
        try:
            out = fn(messages, modele or config.LLM_MODEL, temperature)
            if out and out.strip():
                if soucis:
                    logger.warning(f"[LLM] bascule sur {nom} après : {' | '.join(soucis)}")
                DERNIER.set(f"{nom} · {_MODELES_OK.get(nom) or modele}")
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


_BASES = {"nvidia": "https://integrate.api.nvidia.com/v1",
          "groq": "https://api.groq.com/openai/v1",
          "cerebras": "https://api.cerebras.ai/v1",
          "openrouter": "https://openrouter.ai/api/v1"}


def chat_stream(messages: list, temperature: float = 0.6, niveau: str = "equilibre"):
    """Générateur : produit la réponse token par token (vrai streaming).
    Prend le PREMIER fournisseur de la chaîne du niveau qui sait streamer (API OpenAI).
    Gemini/Ollama ne streament pas ici → repli sur une réponse d'un bloc."""
    from openai import OpenAI
    cles = {"nvidia": getattr(config, "NVIDIA_API_KEY", ""), "groq": config.GROQ_API_KEY,
            "cerebras": config.CEREBRAS_API_KEY,
            "openrouter": getattr(config, "OPENROUTER_API_KEY", "")}
    provider, model, client = None, config.LLM_MODEL, None
    for nom, _fn, mod in _providers_disponibles(niveau):
        if nom in _BASES and cles.get(nom):
            provider = nom
            model = _MODELES_OK.get(nom) or mod
            client = OpenAI(api_key=cles[nom], base_url=_BASES[nom])
            break
    try:
        if client is None:
            # Aucun fournisseur « streamable » → réponse complète d'un coup
            yield chat(messages, temperature=temperature, niveau=niveau); return
        DERNIER.set(f"{provider} · {model}")
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
            yield chat(messages, temperature=temperature, niveau=niveau)
        except Exception as e2:
            yield f"❌ Erreur LLM : {str(e2)[:200]}"


def _groq_chat(messages: list, model: str, temperature: float) -> str:
    """Groq avec auto-guérison : les modèles sont régulièrement retirés (404).
    On essaie le modèle configuré, des noms connus, puis ceux réellement offerts au compte."""
    from groq import Groq
    client = Groq(api_key=config.GROQ_API_KEY)

    candidats = []
    for m in (_MODELES_OK.get("groq"), model, config.GROQ_MODEL,
              "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
              "meta-llama/llama-4-scout-17b-16e-instruct",
              "openai/gpt-oss-120b", "qwen/qwen3-32b", "gemma2-9b-it"):
        if m and m not in candidats:
            candidats.append(m)
    try:                                   # modèles réellement disponibles sur CE compte
        for mo in client.models.list().data:
            mid = getattr(mo, "id", "") or ""
            bas = mid.lower()
            # On exclut ce qui n'est pas un modèle de chat classique.
            # « compound » = systèmes agentiques : ils refusent nos paramètres
            # (« Tool choice is none, but mode… ») et cassaient la conversation.
            if mid and mid not in candidats and not any(
                    k in bas for k in ("whisper", "tts", "guard", "vision", "embed",
                                       "compound", "agent", "rerank", "moderation")):
                candidats.append(mid)
    except Exception:
        pass

    derniere = None
    for m in candidats:
        try:
            resp = client.chat.completions.create(
                model=m, messages=messages, temperature=temperature, max_tokens=4096)
            _MODELES_OK["groq"] = m
            try:
                from llm.usage import record
                record(getattr(getattr(resp, "usage", None), "total_tokens", 0))
            except Exception:
                pass
            return resp.choices[0].message.content or ""
        except Exception as e:
            derniere = e
            t = str(e).lower()
            # Modèle retiré (404) OU incompatible avec un chat simple (400) → on tente le suivant.
            # Sans le cas 400, un seul modèle capricieux faisait échouer tout Groq.
            if any(k in t for k in ("not_found", "does not exist", "404", "decommission",
                                    "no longer", "400", "tool choice", "unsupported",
                                    "invalid_request", "does not support")):
                logger.warning(f"[LLM] Groq : modèle '{m}' inutilisable ({str(e)[:60]}), essai suivant…")
                continue
            raise
    raise RuntimeError(f"Groq : aucun modèle texte accessible ({derniere})")


def _nvidia_chat(messages: list, model: str, temperature: float) -> str:
    """NVIDIA NIM (build.nvidia.com) — API compatible OpenAI, offre gratuite généreuse.
    Auto-guérison : essaie le modèle configuré, des valeurs sûres, puis ceux réellement
    accessibles au compte (le catalogue évolue souvent)."""
    from openai import OpenAI
    if not config.NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY absente.")
    client = OpenAI(api_key=config.NVIDIA_API_KEY,
                    base_url="https://integrate.api.nvidia.com/v1")

    candidats = []
    for m in (_MODELES_OK.get("nvidia"), model, config.NVIDIA_MODEL,
              "meta/llama-3.3-70b-instruct",
              "nvidia/llama-3.3-nemotron-super-49b-v1",
              "meta/llama-3.1-70b-instruct",
              "qwen/qwen2.5-72b-instruct",
              "mistralai/mistral-large-2-instruct",
              "meta/llama-3.1-8b-instruct"):
        if m and m not in candidats:
            candidats.append(m)
    try:
        for mo in client.models.list().data:
            mid = getattr(mo, "id", "") or ""
            bas = mid.lower()
            if mid and mid not in candidats and not any(
                    k in bas for k in ("embed", "rerank", "vision", "ocr", "speech", "tts",
                                       "riva", "guard", "diffusion", "image", "video")):
                candidats.append(mid)
    except Exception:
        pass

    derniere = None
    for m in candidats:
        try:
            resp = client.chat.completions.create(
                model=m, messages=messages, temperature=temperature, max_tokens=4096)
            _MODELES_OK["nvidia"] = m
            try:
                from llm.usage import record
                record(getattr(getattr(resp, "usage", None), "total_tokens", 0), provider="nvidia")
            except Exception:
                pass
            return resp.choices[0].message.content or ""
        except Exception as e:
            derniere = e
            t = str(e).lower()
            if any(k in t for k in ("not_found", "does not exist", "404", "400",
                                    "unsupported", "invalid_request", "not available")):
                logger.warning(f"[LLM] NVIDIA : modèle '{m}' inutilisable, essai suivant…")
                continue
            raise
    raise RuntimeError(f"NVIDIA : aucun modèle accessible ({derniere})")


def _openrouter_chat(messages: list, model: str, temperature: float) -> str:
    """OpenRouter — une clé, des dizaines de modèles (dont beaucoup de gratuits, suffixe « :free »).
    API compatible OpenAI. Sert de filet universel quand les autres fournisseurs tombent."""
    from openai import OpenAI
    if not getattr(config, "OPENROUTER_API_KEY", ""):
        raise RuntimeError("OPENROUTER_API_KEY absente.")
    client = OpenAI(api_key=config.OPENROUTER_API_KEY,
                    base_url="https://openrouter.ai/api/v1")

    candidats = []
    for m in (_MODELES_OK.get("openrouter"), model, config.OPENROUTER_MODEL,
              "meta-llama/llama-3.3-70b-instruct:free",
              "deepseek/deepseek-chat:free",
              "qwen/qwen-2.5-72b-instruct:free",
              "google/gemma-2-9b-it:free",
              "meta-llama/llama-3.2-3b-instruct:free"):
        if m and m not in candidats:
            candidats.append(m)

    derniere = None
    for m in candidats:
        try:
            resp = client.chat.completions.create(
                model=m, messages=messages, temperature=temperature, max_tokens=4096,
                extra_headers={"X-Title": "Nova"})
            _MODELES_OK["openrouter"] = m
            try:
                from llm.usage import record
                record(getattr(getattr(resp, "usage", None), "total_tokens", 0), provider="openrouter")
            except Exception:
                pass
            return resp.choices[0].message.content or ""
        except Exception as e:
            derniere = e
            t = str(e).lower()
            if any(k in t for k in ("not_found", "404", "400", "no endpoints",
                                    "unsupported", "invalid_request", "not available")):
                logger.warning(f"[LLM] OpenRouter : modèle '{m}' inutilisable, essai suivant…")
                continue
            raise
    raise RuntimeError(f"OpenRouter : aucun modèle accessible ({derniere})")


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
            corps = {
                "contents": [{"parts": [{"text": question},
                                        {"inline_data": {"mime_type": mime, "data": b64}}]}],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": 2048},
            }
            r = requests.post(url, headers=_gemini_auth(), json=corps, timeout=90)
            if r.status_code in (400, 401, 403):      # repli : ancienne méthode ?key=
                r = requests.post(url, params={"key": config.GEMINI_API_KEY},
                                  json=corps, timeout=90)
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



def _gemini_auth() -> dict:
    """En-têtes d'authentification Gemini.

    Google émet deux formats de clés : anciennes « AIza… » (paramètre ?key=) et nouvelles
    « AQ.… ». L'en-tête x-goog-api-key fonctionne pour LES DEUX — on l'utilise donc en
    priorité, et on garde ?key= en secours pour les intégrations plus anciennes.
    """
    return {"x-goog-api-key": (config.GEMINI_API_KEY or "").strip(),
            "Content-Type": "application/json"}


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

    # Auto-guérison : les noms de modèles Gemini changent (404). On essaie le modèle
    # configuré, des noms connus, puis ceux réellement offerts par l'API.
    candidats = []
    for m in (_MODELES_OK.get("gemini"), model, config.GEMINI_MODEL,
              "gemini-2.0-flash", "gemini-flash-latest", "gemini-2.5-flash",
              "gemini-1.5-flash", "gemini-2.5-pro"):
        if m and m not in candidats:
            candidats.append(m)
    try:
        lst = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                           headers=_gemini_auth(), timeout=20)
        if lst.status_code == 200:
            for mo in (lst.json().get("models") or []):
                nom = (mo.get("name") or "").replace("models/", "")
                if nom and nom not in candidats and "generateContent" in (mo.get("supportedGenerationMethods") or []):
                    candidats.append(nom)
    except Exception:
        pass

    resp, derniere = None, ""
    for m in candidats:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        r = requests.post(url, headers=_gemini_auth(), json=payload, timeout=120)
        if r.status_code in (400, 401, 403):          # repli : ancienne méthode ?key=
            r = requests.post(url, params={"key": config.GEMINI_API_KEY}, json=payload, timeout=120)
        if r.status_code == 200:
            _MODELES_OK["gemini"] = m
            resp = r
            break
        derniere = f"{r.status_code}: {r.text[:200]}"
        if r.status_code in (404, 400):
            logger.warning(f"[LLM] Gemini : modèle '{m}' indisponible, essai suivant…")
            continue
        break

    if resp is None or resp.status_code != 200:
        # Un 403 avec une clé valide signifie presque toujours que l'API n'est pas activée
        # sur le projet Google (ou que la clé est restreinte) : on le dit clairement.
        d = str(derniere)
        if d.startswith("403"):
            cause = "clé restreinte" if "restrict" in d.lower() or "referer" in d.lower() else \
                    "API « Generative Language » non activée sur le projet Google"
            raise RuntimeError(
                f"Gemini refusé (403) — {cause}.\n"
                "→ Va sur aistudio.google.com, crée la clé depuis « Get API key » en choisissant "
                "un projet, et vérifie que la clé n'a AUCUNE restriction d'application/site.\n"
                f"Détail : {d[:220]}")
        raise RuntimeError(f"Gemini HTTP {d[:260] or 'aucun modèle accessible'}")

    data = resp.json()
    # Consommation Gemini : sans cela, la jauge d'énergie l'ignorait complètement
    try:
        from llm.usage import record
        record(int((data.get("usageMetadata") or {}).get("totalTokenCount", 0) or 0),
               provider="gemini")
    except Exception:
        pass

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
