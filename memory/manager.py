"""
Gestionnaire de mémoire unifié:
 - SQLite: mémoire à court terme (conversations récentes)
 - ChromaDB: mémoire longue durée (semantic search)
 - Auto-résumé: compresse les conversations longues
"""
import logging
from datetime import datetime

from agent.memory import save_message, get_history, clear_history
from memory.chroma_store import ChromaStore
from config import config

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self):
        self.chroma = ChromaStore(config.CHROMA_DIR)
        self._summary_cache: dict[str, str] = {}

    def remember(self, agent_id: str, role: str, content: str):
        """Sauvegarde dans SQLite + index dans ChromaDB."""
        save_message(agent_id, role, content)
        if self.chroma.available:
            self.chroma.add(
                agent_id,
                content,
                metadata={"role": role, "timestamp": datetime.utcnow().isoformat()},
            )

    def recall_recent(self, agent_id: str, limit: int = 20) -> list[dict]:
        """Retourne les N derniers messages (SQLite)."""
        return get_history(agent_id, limit)

    def recall_relevant(self, agent_id: str, query: str, n: int = 5) -> list[dict]:
        """Retrouve les souvenirs les plus pertinents (ChromaDB)."""
        if not self.chroma.available:
            return []
        return self.chroma.search(agent_id, query, n)

    def build_context(self, agent_id: str, current_task: str, recent_limit: int = 10) -> str:
        """
        Construit le contexte complet pour l'agent:
        1. Résumé des échanges passés (si disponible)
        2. Souvenirs pertinents à la tâche courante
        3. Messages récents
        """
        parts = []

        # Résumé long-terme
        if agent_id in self._summary_cache:
            parts.append(f"[CONTEXTE PASSÉ]\n{self._summary_cache[agent_id]}")

        # Mémoire sémantique
        if self.chroma.available:
            relevant = self.recall_relevant(agent_id, current_task, n=3)
            if relevant:
                mem_text = "\n".join(f"- {r['text'][:200]}" for r in relevant)
                parts.append(f"[SOUVENIRS PERTINENTS]\n{mem_text}")

        # Historique récent
        recent = self.recall_recent(agent_id, recent_limit)
        if recent:
            history_text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in recent)
            parts.append(f"[HISTORIQUE RÉCENT]\n{history_text}")

        return "\n\n".join(parts)

    def cache_summary(self, agent_id: str, summary: str):
        self._summary_cache[agent_id] = summary

    def get_summary(self, agent_id: str) -> str | None:
        return self._summary_cache.get(agent_id)

    def should_summarize(self, agent_id: str) -> bool:
        history = get_history(agent_id, limit=config.SUMMARY_THRESHOLD + 1)
        return len(history) >= config.SUMMARY_THRESHOLD

    def clear(self, agent_id: str):
        clear_history(agent_id)
        self._summary_cache.pop(agent_id, None)
        if self.chroma.available:
            self.chroma.delete_collection(agent_id)
