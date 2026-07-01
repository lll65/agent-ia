"""
Mémoire persistante via Supabase (Postgres + pgvector).

Remplace ChromaDB + SQLite quand SUPABASE_DB_URL est défini : la mémoire vit dans
une base Postgres managée, donc elle SURVIT aux redémarrages / mises en veille de
l'hôte (indispensable sur les hébergeurs éphémères type Render free).

Une seule table `agent_memory` sert à la fois :
  - l'historique récent  → ORDER BY id DESC
  - la recherche sémantique → embedding <=> requête (distance cosinus pgvector)

Embeddings générés localement (MiniLM 384-dim), réutilise l'embedder déjà fourni
par chromadb ; fallback sentence-transformers ; sinon la recherche sémantique est
désactivée (l'historique récent, lui, fonctionne toujours).

Dégradation gracieuse : si psycopg2 manque ou la connexion échoue, `available`
reste False et le MemoryManager retombe automatiquement sur SQLite + ChromaDB.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_EMBED_DIM = 384  # MiniLM-L6-v2

# ─── Embedder (lazy, mis en cache) ────────────────────────────────────────────

_embedder = None
_embedder_tried = False


def _get_embedder():
    global _embedder, _embedder_tried
    if _embedder_tried:
        return _embedder
    _embedder_tried = True
    # 1. Réutilise l'embedder onnx léger fourni par chromadb (déjà installé)
    try:
        from chromadb.utils import embedding_functions
        _embedder = ("chroma", embedding_functions.DefaultEmbeddingFunction())
        logger.info("[Supabase] Embeddings via chromadb (MiniLM onnx).")
        return _embedder
    except Exception:
        pass
    # 2. Fallback sentence-transformers
    try:
        from sentence_transformers import SentenceTransformer
        _embedder = ("st", SentenceTransformer("all-MiniLM-L6-v2"))
        logger.info("[Supabase] Embeddings via sentence-transformers.")
        return _embedder
    except Exception:
        pass
    logger.warning("[Supabase] Aucun embedder — recherche sémantique désactivée (historique récent OK).")
    _embedder = None
    return None


def _embed(text: str) -> list[float] | None:
    emb = _get_embedder()
    if emb is None:
        return None
    try:
        kind, model = emb
        if kind == "chroma":
            vec = model([text])[0]
        else:  # sentence-transformers
            vec = model.encode(text)
        return [float(x) for x in vec]
    except Exception as e:
        logger.debug(f"[Supabase] embed error: {e}")
        return None


def _vec_literal(vec: list[float]) -> str:
    """Formate un vecteur au format littéral pgvector : '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(round(float(x), 6)) for x in vec) + "]"


# ─── Store ────────────────────────────────────────────────────────────────────

class SupabaseStore:
    def __init__(self, db_url: str):
        self.db_url = db_url or ""
        self._conn = None
        self.available = False
        if not self.db_url:
            return
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            logger.warning(
                "[Supabase] psycopg2 non installé — backend désactivé. "
                "Installe: pip install psycopg2-binary"
            )
            return
        try:
            self._ensure_schema()
            self.available = True
            logger.info("[Supabase] Backend mémoire persistant actif.")
        except Exception as e:
            logger.warning(f"[Supabase] Init échec (fallback local): {e}")
            self.available = False

    # ── Connexion ────────────────────────────────────────────────────────────

    def _connect(self):
        import psycopg2
        if self._conn is None or getattr(self._conn, "closed", 1):
            self._conn = psycopg2.connect(self.db_url, connect_timeout=15)
            self._conn.autocommit = True
        return self._conn

    def _run(self, sql: str, params: tuple = (), fetch: bool = False):
        """Exécute une requête avec une tentative de reconnexion sur coupure."""
        for attempt in (1, 2):
            try:
                conn = self._connect()
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    if fetch:
                        return cur.fetchall()
                    return None
            except Exception as e:
                # Connexion probablement périmée (hôte serverless) → on reset et réessaie une fois
                self._conn = None
                if attempt == 2:
                    raise
                logger.debug(f"[Supabase] retry après erreur: {e}")

    def _ensure_schema(self):
        self._run("CREATE EXTENSION IF NOT EXISTS vector;")
        self._run(
            f"""
            CREATE TABLE IF NOT EXISTS agent_memory (
                id         BIGSERIAL PRIMARY KEY,
                agent_id   TEXT NOT NULL,
                role       TEXT,
                content    TEXT NOT NULL,
                embedding  vector({_EMBED_DIM}),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        self._run(
            "CREATE INDEX IF NOT EXISTS idx_agent_memory_agent_id "
            "ON agent_memory (agent_id, id DESC);"
        )

    # ── API (miroir de ChromaStore + historique) ─────────────────────────────

    def add(self, agent_id: str, role: str, content: str) -> None:
        """Insère un souvenir (historique + sémantique dans la même ligne)."""
        if not self.available or not content:
            return
        try:
            vec = _embed(content)
            if vec is not None and len(vec) == _EMBED_DIM:
                self._run(
                    "INSERT INTO agent_memory (agent_id, role, content, embedding) "
                    "VALUES (%s, %s, %s, %s::vector)",
                    (agent_id, role, content, _vec_literal(vec)),
                )
            else:
                self._run(
                    "INSERT INTO agent_memory (agent_id, role, content) VALUES (%s, %s, %s)",
                    (agent_id, role, content),
                )
        except Exception as e:
            logger.warning(f"[Supabase] add error: {e}")

    def recent(self, agent_id: str, limit: int = 20) -> list[dict]:
        """Retourne les `limit` derniers messages, ordre chronologique."""
        if not self.available:
            return []
        try:
            rows = self._run(
                "SELECT role, content FROM agent_memory "
                "WHERE agent_id = %s ORDER BY id DESC LIMIT %s",
                (agent_id, int(limit)),
                fetch=True,
            ) or []
            return [{"role": r[0] or "user", "content": r[1]} for r in reversed(rows)]
        except Exception as e:
            logger.warning(f"[Supabase] recent error: {e}")
            return []

    def search(self, agent_id: str, query: str, n: int = 5) -> list[dict]:
        """Recherche sémantique. Retourne [{text, metadata, score}] (comme ChromaStore)."""
        if not self.available:
            return []
        vec = _embed(query)
        if vec is None or len(vec) != _EMBED_DIM:
            return []  # pas d'embedder → sémantique indisponible (l'historique récent couvre)
        try:
            lit = _vec_literal(vec)
            rows = self._run(
                "SELECT content, role, 1 - (embedding <=> %s::vector) AS score "
                "FROM agent_memory "
                "WHERE agent_id = %s AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (lit, agent_id, lit, int(n)),
                fetch=True,
            ) or []
            return [
                {"text": r[0], "metadata": {"role": r[1]}, "score": float(r[2]) if r[2] is not None else 0.0}
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"[Supabase] search error: {e}")
            return []

    def clear(self, agent_id: str) -> None:
        if not self.available:
            return
        try:
            self._run("DELETE FROM agent_memory WHERE agent_id = %s", (agent_id,))
        except Exception as e:
            logger.warning(f"[Supabase] clear error: {e}")

    def count(self, agent_id: str) -> int:
        if not self.available:
            return 0
        try:
            rows = self._run(
                "SELECT COUNT(*) FROM agent_memory WHERE agent_id = %s",
                (agent_id,), fetch=True,
            )
            return int(rows[0][0]) if rows else 0
        except Exception:
            return 0
