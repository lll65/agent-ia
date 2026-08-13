"""
Suivi de la consommation de tokens par fournisseur (Groq, Cerebras…).

Persistance DURABLE via Supabase si SUPABASE_DB_URL est défini (sinon fichier local).
→ le compteur SURVIT aux redémarrages de l'agent (sur Render, data/ est éphémère).
Remise à zéro chaque jour. C'est une ESTIMATION locale (pas le compteur officiel du
fournisseur), mais fiable pour se repérer.
"""
import json
import threading
from datetime import date
from pathlib import Path

from config import config

_FILE = Path("data/groq_usage.json")
LIMITS = {"groq": 100_000, "cerebras": 1_000_000}  # tokens/jour indicatifs (palier gratuit)
_lock = threading.Lock()
_conn = None


def _today() -> str:
    return date.today().isoformat()


def _sb():
    """Connexion Supabase (mise en cache) + création de la table au besoin."""
    global _conn
    if not getattr(config, "SUPABASE_DB_URL", ""):
        return None
    try:
        import psycopg2
        if _conn is None or getattr(_conn, "closed", 1):
            _conn = psycopg2.connect(config.SUPABASE_DB_URL, connect_timeout=10)
            _conn.autocommit = True
            with _conn.cursor() as c:
                c.execute("CREATE TABLE IF NOT EXISTS usage_daily "
                          "(day text, provider text, tokens bigint, PRIMARY KEY (day, provider))")
        return _conn
    except Exception:
        _conn = None
        return None


def _load_file() -> dict:
    if _FILE.exists():
        try:
            d = json.loads(_FILE.read_text(encoding="utf-8"))
            if d.get("date") == _today():
                return d
        except Exception:
            pass
    return {"date": _today()}


def _save_file(d: dict) -> None:
    d["date"] = _today()
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def record(tokens, provider: str = "groq") -> None:
    if not tokens:
        return
    tokens = int(tokens)
    with _lock:
        d = _load_file()
        d[provider] = int(d.get(provider, 0)) + tokens
        _save_file(d)
    conn = _sb()
    if conn:
        try:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO usage_daily (day, provider, tokens) VALUES (%s, %s, %s) "
                    "ON CONFLICT (day, provider) DO UPDATE SET tokens = usage_daily.tokens + EXCLUDED.tokens",
                    (_today(), provider, tokens))
        except Exception:
            pass


def get_usage(provider: str = "groq") -> tuple[int, int]:
    """(tokens_utilisés_aujourdhui, limite). Priorité à Supabase (durable)."""
    conn = _sb()
    if conn:
        try:
            with conn.cursor() as c:
                c.execute("SELECT tokens FROM usage_daily WHERE day = %s AND provider = %s",
                          (_today(), provider))
                row = c.fetchone()
                if row:
                    return int(row[0]), LIMITS.get(provider, 0)
        except Exception:
            pass
    return int(_load_file().get(provider, 0)), LIMITS.get(provider, 0)


DAILY_LIMIT = LIMITS["groq"]  # rétro-compat
