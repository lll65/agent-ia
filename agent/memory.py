import sqlite3
import json
from datetime import datetime
from config import config


def init_db():
    conn = sqlite3.connect(config.DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            system_prompt TEXT NOT NULL,
            tools TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_message(agent_id: str, role: str, content: str):
    conn = sqlite3.connect(config.DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO conversations (agent_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (agent_id, role, content, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_history(agent_id: str, limit: int = 20) -> list[dict]:
    conn = sqlite3.connect(config.DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM conversations WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
        (agent_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def clear_history(agent_id: str):
    conn = sqlite3.connect(config.DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM conversations WHERE agent_id = ?", (agent_id,))
    conn.commit()
    conn.close()


def save_agent(agent_id: str, name: str, description: str, system_prompt: str, tools: list, model: str):
    conn = sqlite3.connect(config.DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT OR REPLACE INTO agents (id, name, description, system_prompt, tools, model, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, name, description, system_prompt, json.dumps(tools), model, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_agent(agent_id: str) -> dict | None:
    conn = sqlite3.connect(config.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, description, system_prompt, tools, model, created_at FROM agents WHERE id = ?", (agent_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "description": row[2],
        "system_prompt": row[3], "tools": json.loads(row[4]),
        "model": row[5], "created_at": row[6],
    }


def list_agents() -> list[dict]:
    conn = sqlite3.connect(config.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, description, model, created_at FROM agents ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "description": r[2], "model": r[3], "created_at": r[4]} for r in rows]
