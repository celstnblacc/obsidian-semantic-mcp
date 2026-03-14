"""
Unit tests for server.py — pytest-compatible, no sys.exit, no real DB/Ollama needed.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Minimal env so server.py imports without crashing
os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")


def _make_mock_conn():
    """Return a (fake_db_conn contextmanager, mock_cur) pair for search_vault tests."""
    from contextlib import contextmanager

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchall.return_value = []
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur

    @contextmanager
    def fake_db_conn():
        yield mock_conn

    return fake_db_conn, mock_cur


# ── embed() ──────────────────────────────────────────────────────────────────

class TestEmbed:
    def test_raises_on_empty_embedding(self, monkeypatch):
        """Ollama returning [] must raise ValueError — not silently produce a bad vector."""
        import requests
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embedding": []}
        mock_resp.raise_for_status = lambda: None
        monkeypatch.setattr(requests, "post", lambda *a, **kw: mock_resp)

        import server
        with pytest.raises(ValueError, match="Empty embedding"):
            server.embed("some content")

    def test_returns_vector_on_success(self, monkeypatch):
        """Valid Ollama response returns the embedding list."""
        import requests
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embedding": [0.1, 0.2, 0.3]}
        mock_resp.raise_for_status = lambda: None
        monkeypatch.setattr(requests, "post", lambda *a, **kw: mock_resp)

        import server
        result = server.embed("some content")
        assert result == [0.1, 0.2, 0.3]

    def test_truncates_to_max_chars(self, monkeypatch):
        """Input longer than MAX_EMBED_CHARS is truncated before sending to Ollama."""
        import requests
        captured = {}

        def fake_post(url, json=None, **kw):
            captured["prompt"] = json.get("prompt", "")
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"embedding": [0.1]}
            mock_resp.raise_for_status = lambda: None
            return mock_resp

        monkeypatch.setattr(requests, "post", fake_post)

        import server
        long_text = "x" * 5000
        server.embed(long_text)
        assert len(captured["prompt"]) <= server.MAX_EMBED_CHARS


# ── VaultEventHandler._handle_upsert ─────────────────────────────────────────

class TestWatchdogHandler:
    def test_db_exception_does_not_kill_thread(self, monkeypatch):
        """DataException from index_note must be caught — watcher thread must survive."""
        import psycopg2
        import server

        def boom(path, content):
            raise psycopg2.errors.DataException("vector must have at least 1 dimension")

        monkeypatch.setattr(server, "index_note", boom)

        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            f.write(b"# test note\n")
            f.flush()
            handler = server.VaultEventHandler()
            # Must not raise — exception must be caught internally
            handler._handle_upsert(f.name)

    def test_generic_exception_does_not_kill_thread(self, monkeypatch):
        """Any exception from index_note must be caught — watcher thread must survive."""
        import server

        def boom(*a):
            raise RuntimeError("boom")
        monkeypatch.setattr(server, "index_note", boom)

        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            f.write(b"# test\n")
            f.flush()
            handler = server.VaultEventHandler()
            handler._handle_upsert(f.name)  # must not raise


# ── _is_system_path ───────────────────────────────────────────────────────────

class TestIsSystemPath:
    def test_skips_obsidian_dir(self, tmp_path):
        """Files inside .obsidian should be skipped."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            p = tmp_path / ".obsidian" / "config.json"
            assert server._is_system_path(p) is True

    def test_skips_trash(self, tmp_path):
        """Files inside .trash should be skipped."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            p = tmp_path / ".trash" / "deleted.md"
            assert server._is_system_path(p) is True

    def test_does_not_skip_normal_note(self, tmp_path):
        """Regular notes should not be skipped."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            p = tmp_path / "notes" / "my_note.md"
            assert server._is_system_path(p) is False

    def test_vault_inside_hidden_dir_not_skipped(self, tmp_path):
        """Notes in a vault that itself lives inside a hidden parent dir must NOT be skipped."""
        hidden_vault = tmp_path / ".vaults" / "my_vault"
        hidden_vault.mkdir(parents=True)
        import server
        with patch.object(server, "VAULT_PATH", str(hidden_vault)):
            p = hidden_vault / "notes" / "note.md"
            assert server._is_system_path(p) is False


# ── indexing_in_progress flag ────────────────────────────────────────────────

class TestIndexingFlag:
    def test_search_returns_indexing_message_when_in_progress(self, monkeypatch):
        """search_vault with empty DB during indexing must say indexing is in progress, not 'try reindex_vault'."""
        import asyncio
        import server
        import threading
        evt = threading.Event()
        evt.set()
        monkeypatch.setattr(server, "_INDEXING_IN_PROGRESS", evt)

        fake_db_conn, _ = _make_mock_conn()
        monkeypatch.setattr(server, "db_conn", fake_db_conn)
        monkeypatch.setattr(server, "embed", lambda q: [0.1, 0.2])

        result = asyncio.run(server.call_tool("search_vault", {"query": "anything"}))
        text = result[0].text
        assert "indexing" in text.lower()
        assert "reindex_vault" not in text


# ── db_conn pool safety ───────────────────────────────────────────────────────

class TestDbConnPoolSafety:
    def test_connection_discarded_on_exception(self, monkeypatch):
        """When the body of db_conn() raises, putconn must be called with close=True
        so the pool discards the connection rather than recycling a broken one."""
        import server

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        monkeypatch.setattr(server, "_pool", mock_pool)

        with pytest.raises(RuntimeError):
            with server.db_conn():
                raise RuntimeError("simulated mid-transaction failure")

        mock_pool.putconn.assert_called_once_with(mock_conn, close=True)

    def test_connection_returned_normally_on_success(self, monkeypatch):
        """On clean exit putconn must be called without close=True."""
        import server

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        monkeypatch.setattr(server, "_pool", mock_pool)

        with server.db_conn():
            pass

        mock_pool.putconn.assert_called_once_with(mock_conn)


# ── input validation — limit / context_length ─────────────────────────────────

class TestSearchInputValidation:
    def test_negative_limit_clamped_to_one(self, monkeypatch):
        """search_vault must not pass a negative LIMIT to PostgreSQL."""
        import asyncio
        import server

        fake_db_conn, mock_cur = _make_mock_conn()
        monkeypatch.setattr(server, "db_conn", fake_db_conn)
        monkeypatch.setattr(server, "embed", lambda q: [0.1])
        monkeypatch.setattr(server, "_INDEXING_IN_PROGRESS", False)

        asyncio.run(server.call_tool("search_vault", {"query": "x", "limit": -99}))

        # SQL uses parameterized queries (%s), so the clamped value is in the
        # params tuple — not the SQL string. Third param is the LIMIT value.
        params = mock_cur.execute.call_args[0][1]
        assert params[-1] >= 1, f"LIMIT must be clamped to ≥1, got {params[-1]}"


# ── _vec_to_str ───────────────────────────────────────────────────────────────

class TestVecToStr:
    def test_formats_correctly(self):
        import server
        result = server._vec_to_str([0.1, 0.2, 0.3])
        assert result == "[0.1,0.2,0.3]"

    def test_empty_raises(self):
        import server
        with pytest.raises(ValueError):
            server._vec_to_str([])


# ── _build_dsn ────────────────────────────────────────────────────────────────

class TestBuildDsn:
    def test_prefers_database_url(self, monkeypatch):
        """DATABASE_URL env var takes priority over POSTGRES_* vars."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://custom/db")
        import config
        assert config.build_dsn() == "postgresql://custom/db"

    def test_falls_back_to_postgres_vars(self, monkeypatch):
        """When DATABASE_URL is absent, assembles DSN from POSTGRES_* vars."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("POSTGRES_HOST", "myhost")
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        monkeypatch.setenv("POSTGRES_DB",   "mydb")
        monkeypatch.setenv("POSTGRES_USER", "myuser")
        monkeypatch.setenv("POSTGRES_PASSWORD", "mypass")
        import config
        dsn = config.build_dsn()
        assert "host=myhost" in dsn
        assert "port=5433" in dsn
        assert "dbname=mydb" in dsn
        assert "user=myuser" in dsn
        assert "password=mypass" in dsn

    def test_fallback_dsn_has_no_credential_url(self, monkeypatch):
        """The libpq keyword format must never produce a postgresql://user:pass@host URL."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        import config
        assert "://" not in config.build_dsn()


# ── _resolve_vault_path ───────────────────────────────────────────────────────

class TestResolveVaultPath:
    def test_allows_nested_path(self, tmp_path):
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            result = server._resolve_vault_path("notes/note.md")
            assert Path(result).is_relative_to(tmp_path.resolve())

    def test_blocks_dotdot_traversal(self, tmp_path):
        """../../etc/passwd must raise ValueError."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            with pytest.raises(ValueError, match="escapes vault"):
                server._resolve_vault_path("../../etc/passwd")

    def test_blocks_absolute_path(self, tmp_path):
        """/etc/passwd must raise ValueError — absolute paths escape the vault."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            with pytest.raises(ValueError, match="escapes vault"):
                server._resolve_vault_path("/etc/passwd")

    def test_vault_root_itself_is_allowed(self, tmp_path):
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            result = server._resolve_vault_path(".")
            assert result == tmp_path.resolve()


# ── file_hash ─────────────────────────────────────────────────────────────────

class TestFileHash:
    def test_deterministic(self):
        import server
        assert server.file_hash("hello world") == server.file_hash("hello world")

    def test_different_inputs_differ(self):
        import server
        assert server.file_hash("hello") != server.file_hash("world")

    def test_returns_string(self):
        import server
        assert isinstance(server.file_hash("x"), str)
