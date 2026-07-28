from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import DATABASE_URL

# prepare_threshold=None desactiva los prepared statements: obligatorio para el
# pooler de Supabase en modo transaction (pgbouncer), que no los soporta.
# El pool se abre de forma perezosa en init_db() para no conectar al importar.
_pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=5,
    kwargs={"row_factory": dict_row, "prepare_threshold": None},
    open=False,
)


def init_db() -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no está configurada")
    _pool.open()
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        BIGINT PRIMARY KEY,
                username       TEXT,
                first_name     TEXT,
                first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
                total_requests INTEGER NOT NULL DEFAULT 0,
                max_resolution INTEGER
            )
        """)


@contextmanager
def _connect():
    # El context manager del pool hace commit al salir limpio y rollback si hay
    # excepción, y devuelve la conexión al pool en ambos casos.
    with _pool.connection() as conn:
        yield conn


def upsert_user(user_id: int, username: str | None, first_name: str | None) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_seen, total_requests)
            VALUES (%s, %s, %s, now(), 1)
            ON CONFLICT (user_id) DO UPDATE SET
                username       = excluded.username,
                first_name     = excluded.first_name,
                last_seen      = now(),
                total_requests = users.total_requests + 1
        """, (user_id, username, first_name))


def get_user(user_id: int) -> dict | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()


def get_all_users(page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """Devuelve (usuarios, total) para la página dada, ordenados por última actividad."""
    offset = (page - 1) * page_size
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM users ORDER BY last_seen DESC LIMIT %s OFFSET %s",
            (page_size, offset),
        ).fetchall()
    return list(rows), total


def get_user_max_resolution(user_id: int) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT max_resolution FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
        return row["max_resolution"] if row else None


def set_user_max_resolution(user_id: int, height: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (user_id, max_resolution) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET max_resolution = excluded.max_resolution",
            (user_id, height),
        )


def clear_user_max_resolution(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET max_resolution = NULL WHERE user_id = %s",
            (user_id,),
        )


def get_stats() -> dict:
    with _connect() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                         AS total_users,
                COALESCE(SUM(total_requests), 0) AS total_requests
            FROM users
        """).fetchone()
        return {"total_users": row["total_users"], "total_requests": row["total_requests"]}
