"""
Self-healing: retry automatique avec backoff exponentiel sur les outils et appels LLM.
Utilise tenacity pour une robustesse maximale.
"""
import logging
import functools
from typing import Callable, Any

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryError,
)

logger = logging.getLogger(__name__)


# Décorateur principal : 3 essais, attente 1s → 2s → 4s
def with_retry(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 8.0,
    exceptions: tuple = (Exception,),
):
    def decorator(fn: Callable) -> Callable:
        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
            retry=retry_if_exception_type(exceptions),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=False,
        )
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            return await fn(*args, **kwargs)

        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
            retry=retry_if_exception_type(exceptions),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=False,
        )
        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        import asyncio
        if asyncio.iscoroutinefunction(fn):
            return async_wrapper
        return sync_wrapper

    return decorator


def safe_tool_call(plugin_loader, tool_name: str, params: dict, fallback: str = "") -> str:
    """
    Exécute un outil avec retry + fallback si tout échoue.
    """
    last_error = None
    for attempt in range(1, 4):
        try:
            result = plugin_loader.run(tool_name, params)
            if not result.startswith("[Plugin") or "Erreur" not in result:
                return result
            last_error = result
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Tool '{tool_name}' tentative {attempt} échouée: {e}")

    msg = f"[Self-heal] Outil '{tool_name}' a échoué {3} fois. Dernière erreur: {last_error}"
    logger.error(msg)
    return fallback or msg


class HealthMonitor:
    """Enregistre les succès/échecs par outil pour détecter les problèmes récurrents."""

    def __init__(self):
        self._stats: dict[str, dict] = {}

    def record(self, tool_name: str, success: bool):
        if tool_name not in self._stats:
            self._stats[tool_name] = {"ok": 0, "fail": 0}
        key = "ok" if success else "fail"
        self._stats[tool_name][key] += 1

    def error_rate(self, tool_name: str) -> float:
        s = self._stats.get(tool_name, {})
        total = s.get("ok", 0) + s.get("fail", 0)
        if total == 0:
            return 0.0
        return s.get("fail", 0) / total

    def report(self) -> dict:
        return {
            name: {**s, "error_rate": f"{self.error_rate(name):.0%}"}
            for name, s in self._stats.items()
        }


health_monitor = HealthMonitor()
