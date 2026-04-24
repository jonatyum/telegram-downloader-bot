import pytest
from unittest.mock import patch
from database import init_db, upsert_user, get_user, get_stats


@pytest.fixture(autouse=True)
def in_memory_db(tmp_path):
    db_file = str(tmp_path / "test.db")
    with patch("database.DB_PATH", db_file):
        init_db()
        yield


class TestInitDb:
    def test_creates_users_table(self, tmp_path):
        import sqlite3
        db_file = str(tmp_path / "check.db")
        with patch("database.DB_PATH", db_file):
            init_db()
            conn = sqlite3.connect(db_file)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            assert ("users",) in tables
            conn.close()

    def test_idempotent(self):
        # Llamar init_db dos veces no lanza error
        init_db()
        init_db()


class TestUpsertUser:
    def test_inserts_new_user(self):
        upsert_user(111, "testuser", "Test")
        user = get_user(111)
        assert user is not None
        assert user["user_id"] == 111
        assert user["username"] == "testuser"
        assert user["first_name"] == "Test"
        assert user["total_requests"] == 1

    def test_updates_existing_user(self):
        upsert_user(111, "oldname", "Old")
        upsert_user(111, "newname", "New")
        user = get_user(111)
        assert user["username"] == "newname"
        assert user["first_name"] == "New"
        assert user["total_requests"] == 2

    def test_increments_total_requests(self):
        upsert_user(222, "user", "Name")
        upsert_user(222, "user", "Name")
        upsert_user(222, "user", "Name")
        assert get_user(222)["total_requests"] == 3

    def test_accepts_none_username(self):
        upsert_user(333, None, "NoUsername")
        user = get_user(333)
        assert user["username"] is None
        assert user["first_name"] == "NoUsername"


class TestGetUser:
    def test_returns_none_for_unknown_user(self):
        assert get_user(999999) is None

    def test_returns_row_for_known_user(self):
        upsert_user(444, "known", "Known")
        assert get_user(444) is not None


class TestGetStats:
    def test_zero_stats_on_empty_db(self):
        stats = get_stats()
        assert stats["total_users"] == 0
        assert stats["total_requests"] == 0

    def test_counts_users_and_requests(self):
        upsert_user(1, "a", "A")
        upsert_user(1, "a", "A")
        upsert_user(2, "b", "B")
        stats = get_stats()
        assert stats["total_users"] == 2
        assert stats["total_requests"] == 3
