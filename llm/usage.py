"""
Suivi de la consommation de tokens par fournisseur (Groq, Cerebras…).
Persisté dans data/groq_usage.json, remis à zéro chaque jour.
"""
import json
from datetime import date
from pathlib import Path

_FILE = Path("data/groq_usage.json")

# Limites journalières indicatives du palier gratuit (tokens/jour)
LIMITS = {"groq": 100_000, "cerebras": 1_000_000}


def _load() -> dict:
    today = date.today().isoformat()
    if _FILE.exists():
        try:
            d = json.loads(_FILE.read_text(encoding="utf-8"))
            if d.get("date") == today:
                return d
        except Exception:
            pass
    return {"date": today}


def _save(d: dict) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def record(tokens, provider: str = "groq") -> None:
    if not tokens:
        return
    d = _load()
    d[provider] = int(d.get(provider, 0)) + int(tokens)
    _save(d)


def get_usage(provider: str = "groq") -> tuple[int, int]:
    """Retourne (tokens_utilisés_aujourdhui, limite) pour un fournisseur."""
    return int(_load().get(provider, 0)), LIMITS.get(provider, 0)


# Rétro-compat
DAILY_LIMIT = LIMITS["groq"]
