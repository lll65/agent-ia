"""
PEA Watcher — surveillance autonome des positions et alertes proactives Telegram.

Boucle en arrière-plan : à intervalle régulier, scanne une liste de valeurs et
pousse une alerte Telegram quand un seuil est franchi (RSI survendu/suracheté,
gros mouvement journalier). Anti-spam intégré : une même règle sur une même valeur
n'est ré-alertée qu'après WATCHER_QUIET_HOURS.

Sources de la watchlist (par ordre de priorité) :
  1. data/watchlist.txt        — un ticker/nom par ligne (le plus simple)
  2. data/portfolios.json      — agrège les tickers de tous les portefeuilles sauvegardés

Test manuel sans Telegram :
    python -m agent.pea_watcher            # scan unique, affiche les alertes (dry-run)
    python -m agent.pea_watcher --send     # scan unique + envoi Telegram réel
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)

_WATCHLIST_FILE = Path("data/watchlist.txt")
_PORTFOLIOS_FILE = Path("data/portfolios.json")
_STATE_FILE = Path("data/watcher_state.json")


# ─── Watchlist ────────────────────────────────────────────────────────────────

def _load_watchlist() -> list[str]:
    """Retourne la liste des tickers à surveiller (résolus en symboles Yahoo)."""
    from plugins.builtin.finance import _resolve_ticker

    raw_entries: list[str] = []

    # Source 1 : watchlist.txt (une entrée par ligne, # = commentaire)
    if _WATCHLIST_FILE.exists():
        for line in _WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                raw_entries.append(line)

    # Source 2 : portefeuilles sauvegardés (premier token de chaque ligne de position)
    if not raw_entries and _PORTFOLIOS_FILE.exists():
        try:
            pfs = json.loads(_PORTFOLIOS_FILE.read_text(encoding="utf-8"))
            for pf in pfs.values():
                for line in (pf.get("positions") or "").splitlines():
                    line = line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    parts = line.split()
                    # Retire les tokens numériques de fin (qté, prix) pour ne garder que le nom
                    name_parts = list(parts)
                    while name_parts:
                        try:
                            float(name_parts[-1].replace(",", "."))
                            name_parts.pop()
                        except ValueError:
                            break
                    if name_parts:
                        raw_entries.append(" ".join(name_parts))
        except Exception as e:
            logger.warning(f"[Watcher] Lecture portfolios.json: {e}")

    # Résolution nom → ticker + déduplication en préservant l'ordre
    resolved: list[str] = []
    for raw in raw_entries:
        tk = _resolve_ticker(raw)
        if tk and tk not in resolved:
            resolved.append(tk)
    return resolved


# ─── État (anti-spam) ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _should_alert(state: dict, ticker: str, rule: str) -> bool:
    """True si (ticker, rule) n'a pas été alerté depuis WATCHER_QUIET_HOURS."""
    key = f"{ticker}|{rule}"
    last = state.get(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return datetime.utcnow() - last_dt >= timedelta(hours=config.WATCHER_QUIET_HOURS)
    except Exception:
        return True


def _mark_alerted(state: dict, ticker: str, rule: str) -> None:
    state[f"{ticker}|{rule}"] = datetime.utcnow().isoformat()


# ─── Évaluation des règles ────────────────────────────────────────────────────

def _evaluate(ticker: str) -> list[tuple[str, str]]:
    """
    Récupère les données réelles d'un ticker et retourne la liste des alertes
    déclenchées : [(rule_id, message_ligne), ...]. Liste vide si rien à signaler.
    """
    from agent.finance_deep import lookup_any_ticker

    look = lookup_any_ticker(ticker)
    if not look.get("found"):
        logger.debug(f"[Watcher] {ticker} introuvable")
        return []

    resolved = look["ticker"]
    name     = look.get("name", resolved)
    price    = look.get("price")
    cur      = look.get("currency", "")
    chg      = look.get("chg_24h")
    levels   = look.get("levels") or {}
    rsi      = levels.get("rsi")

    alerts: list[tuple[str, str]] = []
    head = f"{name} ({resolved}) — {price:.2f}{cur}"

    # Règle 1 : RSI survendu → zone d'achat
    if rsi is not None and rsi <= config.WATCHER_RSI_LOW:
        alerts.append((
            "rsi_low",
            f"🟢 {head}\n   RSI {rsi:.0f} ≤ {config.WATCHER_RSI_LOW:.0f} → SURVENDU, zone d'achat potentielle"
            + (f" | j: {chg:+.1f}%" if chg is not None else ""),
        ))

    # Règle 2 : RSI suracheté → prendre profits
    if rsi is not None and rsi >= config.WATCHER_RSI_HIGH:
        alerts.append((
            "rsi_high",
            f"🔴 {head}\n   RSI {rsi:.0f} ≥ {config.WATCHER_RSI_HIGH:.0f} → SURACHETÉ, envisager prise de profits"
            + (f" | j: {chg:+.1f}%" if chg is not None else ""),
        ))

    # Règle 3 : gros mouvement journalier
    if chg is not None and abs(chg) >= config.WATCHER_MOVE_PCT:
        direction = "📈 HAUSSE" if chg > 0 else "📉 BAISSE"
        rsi_str = f" | RSI {rsi:.0f}" if rsi is not None else ""
        alerts.append((
            "big_move",
            f"⚡ {head}\n   {direction} {chg:+.1f}% aujourd'hui{rsi_str}",
        ))

    return alerts


# ─── Scan complet ─────────────────────────────────────────────────────────────

def run_once(dry_run: bool = False) -> dict:
    """
    Effectue un scan complet de la watchlist.
    dry_run=True : n'envoie rien sur Telegram, retourne juste les alertes.
    Retourne {"tickers": N, "alerts": [messages], "sent": bool}.
    """
    watchlist = _load_watchlist()
    if not watchlist:
        logger.info("[Watcher] Watchlist vide (crée data/watchlist.txt ou sauvegarde un portefeuille).")
        return {"tickers": 0, "alerts": [], "sent": False}

    logger.info(f"[Watcher] Scan de {len(watchlist)} valeurs: {', '.join(watchlist)}")
    state = _load_state()
    fresh_alerts: list[str] = []

    for ticker in watchlist:
        try:
            for rule, msg in _evaluate(ticker):
                if _should_alert(state, ticker, rule):
                    fresh_alerts.append(msg)
                    if not dry_run:
                        _mark_alerted(state, ticker, rule)
        except Exception as e:
            logger.warning(f"[Watcher] Erreur sur {ticker}: {e}")

    sent = False
    if fresh_alerts and not dry_run:
        header = f"🔔 Alerte PEA — {datetime.now().strftime('%d/%m %H:%M')}\n\n"
        body = header + "\n\n".join(fresh_alerts)
        body += "\n\n_Surveillance automatique • ajuste les seuils dans .env_"
        from bots.telegram_push import send_message
        sent = send_message(body)
        if sent:
            _save_state(state)
            logger.info(f"[Watcher] {len(fresh_alerts)} alerte(s) envoyée(s) sur Telegram.")
        else:
            logger.warning("[Watcher] Alertes non envoyées (Telegram indisponible) — état non marqué.")

    return {"tickers": len(watchlist), "alerts": fresh_alerts, "sent": sent}


# ─── Boucle asynchrone (intégrée au serveur) ──────────────────────────────────

async def watch_loop() -> None:
    """Boucle de surveillance — lancée par main.py si WATCHER_ENABLED."""
    interval = max(300, config.WATCHER_INTERVAL)  # plancher 5 min pour éviter le rate-limit Yahoo
    logger.info(f"🔔 PEA Watcher démarré (scan toutes les {interval//60} min).")
    # Petit délai initial pour laisser le serveur finir de démarrer
    await asyncio.sleep(20)
    while True:
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, run_once, False)
            if result["alerts"]:
                logger.info(f"[Watcher] {len(result['alerts'])} alerte(s) ce cycle.")
        except asyncio.CancelledError:
            logger.info("PEA Watcher: arrêt propre.")
            raise
        except Exception as e:
            logger.error(f"[Watcher] Erreur boucle: {e}", exc_info=True)
        await asyncio.sleep(interval)


# ─── Exécution manuelle (test) ────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    send = "--send" in sys.argv
    print(f"=== PEA Watcher — scan unique ({'ENVOI RÉEL' if send else 'DRY-RUN'}) ===\n")
    res = run_once(dry_run=not send)
    print(f"\nValeurs surveillées : {res['tickers']}")
    if not res["alerts"]:
        print("Aucune alerte déclenchée (aucun seuil franchi, ou watchlist vide).")
    else:
        print(f"Alertes ({len(res['alerts'])}) :\n")
        for a in res["alerts"]:
            print(a + "\n")
    if send:
        print(f"Envoi Telegram : {'✅ réussi' if res['sent'] else '❌ échec (voir logs)'}")
    else:
        print("(dry-run : rien n'a été envoyé. Relance avec --send pour pousser sur Telegram.)")
