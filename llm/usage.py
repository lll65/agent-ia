"""
Suivi de la consommation de tokens Groq (limite gratuite ~100 000 tokens/jour).
Persisté dans data/groq_usage.json, remis à zéro chaque jour.
"""
import json
from datetime import date
from pathlib import Path

_FILE = Path("data/groq_usage.json")
DAILY_LIMIT = 100_000


def _load() -> dict:
    today = date.today().isoformat()
    if _FILE.exists():
        try:
            d = json.loads(_FILE.read_text(encoding="utf-8"))
            if d.get("date") == today:
                return d
        except Exception:
            pass
    return {"date": today, "used": 0}


def _save(d: dict) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def record(tokens) -> None:
    if not tokens:
        return
    d = _load()
    d["used"] = int(d.get("used", 0)) + int(tokens)
    _save(d)


def get_usage() -> tuple[int, int]:
    """Retourne (tokens_utilisés_aujourdhui, limite)."""
    return int(_load().get("used", 0)), DAILY_LIMIT
