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
# Depuis quand ce compteur compte. Sur Render sans Supabase, c'est le démarrage du
# conteneur — pas le début de la journée. Voir durable() : la nuance change tout.
_DEPART = __import__("time").time()
LIMITS = {"groq": 100_000, "cerebras": 1_000_000, "gemini": 1_000_000, "nvidia": 1_000_000}  # tokens/jour indicatifs (palier gratuit)
_lock = threading.Lock()
_conn = None


def _today() -> str:
    return date.today().isoformat()


def durable() -> bool:
    """Ce compteur survit-il à un redémarrage ?

    ⚠️ « c'est faux ce qui y a écrit non ? » — il regardait « Groq 241/200k » après une
    journée entière d'utilisation. Il avait raison, et la cause est écrite en tête de ce
    module : sans SUPABASE_DB_URL, la consommation vit dans data/groq_usage.json, sur un
    disque que Render EFFACE. L'offre gratuite endort l'instance au bout de ~15 min sans
    requête ; au réveil, le conteneur est neuf et le compteur repart à zéro.

    La jauge affichait donc « 100 % » en permanence — non pas parce qu'il n'avait rien
    consommé, mais parce qu'elle avait oublié. C'est exactement ce qu'il redoutait quand
    il a posé sa condition : « pour ça faut vraiment que la limite dans la fiole soit
    fiable ». Elle ne l'est pas encore, et il faut le DIRE plutôt que d'afficher un
    chiffre rassurant.
    """
    return _sb() is not None


def compte_depuis() -> float:
    """Heures écoulées depuis le début REEL de la mesure."""
    import time as _t
    if durable():
        # Supabase garde la journée entière : la mesure commence à minuit.
        from datetime import datetime
        n = datetime.now()
        return n.hour + n.minute / 60.0
    return max(0.0, (_t.time() - _DEPART) / 3600.0)


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
