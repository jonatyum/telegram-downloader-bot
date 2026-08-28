"""
Tests de database.py contra la implementación Postgres (psycopg + pool).

No hay Postgres en la suite, así que la conexión está mockeada y lo que se verifica
es el contrato con la BD: qué SQL se emite, con qué parámetros y cómo se mapea la
fila de vuelta. La semántica del propio SQL (el upsert que incrementa, el ON CONFLICT)
vive en el servidor y no se puede ejercitar aquí — eso lo cubre el entorno real.
"""
import re
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from database import (
    init_db,
    upsert_user,
    get_user,
    get_all_users,
    get_user_max_resolution,
    set_user_max_resolution,
    clear_user_max_resolution,
    get_stats,
)


def _norm(sql: str) -> str:
    """SQL en una línea y en minúsculas, para poder aseverar sin pelearse con el indentado."""
    return re.sub(r"\s+", " ", sql).strip().lower()


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Conexión de mentira que registra cada execute y sirve filas preprogramadas."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple | None]] = []
        self._results = list(results or [])

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        rows = self._results.pop(0) if self._results else []
        return FakeCursor(rows)

    # Azúcar para las aserciones
    @property
    def sql(self) -> list[str]:
        return [_norm(s) for s, _ in self.calls]

    @property
    def params(self) -> list:
        return [p for _, p in self.calls]


@contextmanager
def _fake_connect(conn):
    yield conn


@pytest.fixture
def conn():
    """Parchea database._connect para que todas las funciones usen la conexión falsa."""
    fake = FakeConn()
    with patch("database._connect", lambda: _fake_connect(fake)):
        yield fake


@pytest.fixture
def rows():
    """Igual que `conn`, pero permite preprogramar los resultados de cada execute."""
    def _make(*results):
        fake = FakeConn(results)
        patcher = patch("database._connect", lambda: _fake_connect(fake))
        patcher.start()
        _make.patchers.append(patcher)
        return fake

    _make.patchers = []
    yield _make
    for p in _make.patchers:
        p.stop()


class TestInitDb:
    def test_raises_without_database_url(self):
        with patch("database.DATABASE_URL", ""):
            with pytest.raises(RuntimeError, match="DATABASE_URL"):
                init_db()

    def test_opens_pool_and_creates_table(self, conn):
        pool = MagicMock()
        with patch("database.DATABASE_URL", "postgresql://user:pw@host:6543/db"), \
             patch("database._pool", pool):
            init_db()

        # El pool se abre perezosamente aquí, no al importar el módulo.
        pool.open.assert_called_once()
        assert "create table if not exists users" in conn.sql[0]

    def test_idempotent(self, conn):
        with patch("database.DATABASE_URL", "postgresql://x"), patch("database._pool", MagicMock()):
            init_db()
            init_db()
        assert all("if not exists" in s for s in conn.sql)


class TestUpsertUser:
    def test_inserts_with_upsert_and_params(self, conn):
        upsert_user(111, "testuser", "Test")

        sql = conn.sql[0]
        assert "insert into users" in sql
        assert "on conflict (user_id) do update" in sql
        assert conn.params[0] == (111, "testuser", "Test")

    def test_increments_total_requests_on_conflict(self, conn):
        upsert_user(111, "testuser", "Test")
        # El contador lo lleva el propio SQL, no Python.
        assert "total_requests = users.total_requests + 1" in conn.sql[0]
        assert "last_seen = now()" in conn.sql[0]

    def test_accepts_none_username(self, conn):
        upsert_user(333, None, "NoUsername")
        assert conn.params[0] == (333, None, "NoUsername")


class TestGetUser:
    def test_returns_none_for_unknown_user(self, rows):
        c = rows([])  # sin filas
        assert get_user(999999) is None
        assert c.params[0] == (999999,)

    def test_returns_row_for_known_user(self, rows):
        c = rows([{"user_id": 444, "username": "known", "first_name": "Known", "total_requests": 3}])
        user = get_user(444)
        assert user["user_id"] == 444
        assert user["username"] == "known"
        assert "where user_id = %s" in c.sql[0]


class TestGetAllUsers:
    def test_returns_rows_and_total(self, rows):
        users = [{"user_id": 1, "username": "a"}, {"user_id": 2, "username": "b"}]
        c = rows([{"c": 2}], users)  # 1º: COUNT, 2º: SELECT

        result, total = get_all_users(page=1, page_size=20)

        assert total == 2
        assert [u["user_id"] for u in result] == [1, 2]
        assert "count(*)" in c.sql[0]

    def test_orders_by_last_seen_desc(self, rows):
        c = rows([{"c": 0}], [])
        get_all_users()
        assert "order by last_seen desc" in c.sql[1]

    def test_paginates_with_offset(self, rows):
        c = rows([{"c": 45}], [])
        get_all_users(page=3, page_size=20)
        # offset = (page - 1) * page_size
        assert c.params[1] == (20, 40)

    def test_first_page_has_zero_offset(self, rows):
        c = rows([{"c": 5}], [])
        get_all_users(page=1, page_size=20)
        assert c.params[1] == (20, 0)


class TestMaxResolution:
    def test_returns_none_for_unknown_user(self, rows):
        rows([])
        assert get_user_max_resolution(999) is None

    def test_returns_stored_value(self, rows):
        rows([{"max_resolution": 720}])
        assert get_user_max_resolution(444) == 720

    def test_returns_none_when_column_is_null(self, rows):
        # El usuario existe pero nunca eligió resolución.
        rows([{"max_resolution": None}])
        assert get_user_max_resolution(444) is None

    def test_set_upserts_value(self, conn):
        set_user_max_resolution(444, 480)
        assert "on conflict (user_id) do update set max_resolution" in conn.sql[0]
        assert conn.params[0] == (444, 480)

    def test_clear_nulls_the_column(self, conn):
        clear_user_max_resolution(444)
        assert "set max_resolution = null" in conn.sql[0]
        assert conn.params[0] == (444,)


class TestGetStats:
    def test_zero_stats_on_empty_db(self, rows):
        rows([{"total_users": 0, "total_requests": 0}])
        stats = get_stats()
        assert stats == {"total_users": 0, "total_requests": 0}

    def test_counts_users_and_requests(self, rows):
        rows([{"total_users": 2, "total_requests": 3}])
        stats = get_stats()
        assert stats["total_users"] == 2
        assert stats["total_requests"] == 3

    def test_coalesces_null_sum(self, rows):
        # Sin usuarios, SUM devuelve NULL: el COALESCE del SQL lo convierte en 0.
        c = rows([{"total_users": 0, "total_requests": 0}])
        get_stats()
        assert "coalesce(sum(total_requests), 0)" in c.sql[0]
