"""
Conversation memory in a SQLite file (SQLITE_PATH, default data/chatbot.sqlite).

Tables:
  sessions  - one row per chat session
  messages  - every user question and bot answer (answer text + small metadata, no result tables)
  context   - the current "conversation context" of a session as JSON: last plant/metric/period,
              or a pending clarifying question - this is what makes follow-ups work
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from app.config import PROJECT_ROOT, settings

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, created_at TEXT, last_active TEXT, user_id TEXT);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
                                     text TEXT, meta TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS context (session_id TEXT PRIMARY KEY, data TEXT, updated_at TEXT);
"""


class MemoryStore:
    def __init__(self, path: str | Path | None = None):
        path = Path(path or settings.sqlite_path)
        if not path.is_absolute() and str(path) != ":memory:":
            path = PROJECT_ROOT / path
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def new_session(self, user_id: str | None = None) -> str:
        sid = uuid.uuid4().hex
        with _lock:
            self.conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (sid, self._now(), self._now(), user_id))
            self.conn.commit()
        return sid

    def ensure_session(self, session_id: str | None, user_id: str | None = None) -> str:
        if session_id:
            row = self.conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row:
                return session_id
        return self.new_session(user_id)

    def add_message(self, session_id: str, role: str, text: str, meta: dict | None = None) -> None:
        with _lock:
            self.conn.execute("INSERT INTO messages (session_id, role, text, meta, created_at) VALUES (?, ?, ?, ?, ?)",
                              (session_id, role, text, json.dumps(meta or {}, default=str), self._now()))
            self.conn.execute("UPDATE sessions SET last_active = ? WHERE id = ?", (self._now(), session_id))
            self.conn.commit()

    def recent_messages(self, session_id: str, limit: int = 6) -> list[dict]:
        rows = self.conn.execute("SELECT role, text FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                                 (session_id, limit)).fetchall()
        return [{"role": r, "text": t} for r, t in reversed(rows)]

    def get_context(self, session_id: str) -> dict:
        row = self.conn.execute("SELECT data FROM context WHERE session_id = ?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_context(self, session_id: str, data: dict) -> None:
        with _lock:
            self.conn.execute("INSERT OR REPLACE INTO context VALUES (?, ?, ?)",
                              (session_id, json.dumps(data, default=str), self._now()))
            self.conn.commit()


_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


def set_store(store: MemoryStore) -> None:
    """Tests: use an in-memory store."""
    global _store
    _store = store
