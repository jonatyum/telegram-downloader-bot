import sqlite3
from contextlib import contextmanager
from config import DB_PATH


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen  TEXT NOT NULL DEFAULT (datetime('now')),
                total_requests INTEGER NOT NULL DEFAULT 0
            )
        """)


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_user(user_id: int, username: str | None, first_name: str | None) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_seen, total_requests)
            VALUES (?, ?, ?, datetime('now'), 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username       = excluded.username,
                first_name     = excluded.first_name,
                last_seen      = datetime('now'),
                total_requests = total_requests + 1
        """, (user_id, username, first_name))


def get_user(user_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def get_stats() -> dict:
    with _connect() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                    AS total_users,
                COALESCE(SUM(total_requests), 0) AS total_requests
            FROM users
        """).fetchone()
        return {"total_users": row["total_users"], "total_requests": row["total_requests"]}
