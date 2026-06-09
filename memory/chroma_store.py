"""
Mémoire vectorielle persistante via ChromaDB.
Stocke et retrouve les souvenirs par similarité sémantique.
Tourne 100% en local — aucune API externe.
"""
import logging
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class ChromaStore:
    def __init__(self, persist_dir: str):
        self._dir = persist_dir
        self._client = None
        self._collections: dict = {}
        self._init()

    def _init(self):
        try:
            import chromadb
            Path(self._dir).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self._dir)
            logger.info(f"ChromaDB initialisé: {self._dir}")
        except Exception as e:
            logger.warning(f"ChromaDB indisponible, fallback SQLite: {e}")
            self._client = None

    def _get_collection(self, agent_id: str):
        if self._client is None:
            return None
        key = f"agent_{agent_id}"
        if key not in self._collections:
            self._collections[key] = self._client.get_or_create_collection(
                name=key,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collections[key]

    def add(self, agent_id: str, text: str, metadata: dict | None = None) -> str:
        """Ajoute un souvenir. Retourne l'ID du document."""
        col = self._get_collection(agent_id)
        if col is None:
            return ""
        doc_id = str(uuid.uuid4())
        meta = {"timestamp": datetime.utcnow().isoformat(), "agent_id": agent_id}
        if metadata:
            meta.update(metadata)
        try:
            col.add(documents=[text], metadatas=[meta], ids=[doc_id])
        except Exception as e:
            logger.warning(f"ChromaDB add error: {e}")
        return doc_id

    def search(self, agent_id: str, query: str, n: int = 5, where: dict | None = None) -> list[dict]:
        """Retrouve les N souvenirs les plus similaires à la requête."""
        col = self._get_collection(agent_id)
        if col is None:
            return []
        # ChromaDB lève une erreur si n_results <= 0 (collection vide ou n négatif)
        limit = min(n, col.count())
        if limit <= 0:
            return []
        try:
            kwargs = {"query_texts": [query], "n_results": limit}
            if where:
                kwargs["where"] = where
            results = col.query(**kwargs)
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]
            return [
                {"text": d, "metadata": m, "score": 1 - dist}
                for d, m, dist in zip(docs, metas, distances)
            ]
        except Exception as e:
            logger.warning(f"ChromaDB search error: {e}")
            return []

    def count(self, agent_id: str) -> int:
        col = self._get_collection(agent_id)
        if col is None:
            return 0
        try:
            return col.count()
        except Exception:
            return 0

    def delete_collection(self, agent_id: str):
        if self._client is None:
            return
        key = f"agent_{agent_id}"
        try:
            self._client.delete_collection(key)
            self._collections.pop(key, None)
        except Exception:
            pass

    @property
    def available(self) -> bool:
        return self._client is not None
