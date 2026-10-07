"""Thin SQLite layer. A new short-lived connection per transaction keeps it
thread-safe for the worker pool; WAL mode lets readers and writers coexist.
The SQL is portable enough to move to PostgreSQL (swap placeholders / driver)."""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    event_name       TEXT NOT NULL,
    issuer           TEXT NOT NULL,
    issue_date       TEXT NOT NULL,
    idempotency_key  TEXT UNIQUE,
    total            INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certificates (
    id               TEXT PRIMARY KEY,
    batch_id         TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    row_index        INTEGER NOT NULL,            -- position in the submitted list
    recipient_name   TEXT,
    recipient_email  TEXT,
    achievement      TEXT,
    status           TEXT NOT NULL
        CHECK (status IN ('pending','processing','completed','failed','invalid')),
    error            TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    code             TEXT UNIQUE,                 -- public certificate number
    file_path        TEXT,
    created_at       TEXT NOT NULL,
    completed_at     TEXT,
    UNIQUE (batch_id, row_index)
);

CREATE INDEX IF NOT EXISTS idx_cert_batch_status ON certificates (batch_id, status);
"""


class Database:
    def __init__(self, path: str):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def tx(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self):
        with self.tx() as conn:
            conn.executescript(SCHEMA)
