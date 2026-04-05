"""Unit tests for app_core.db.get_db_connection behavior (commit/rollback/pragma/params)."""

import sqlite3
import types
import pytest

import app_core.db as db_mod

def test_get_db_connection_commits_and_closes(monkeypatch):
    calls = {"execs": [], "committed": 0, "closed": 0, "connect_args": None}

    class FakeConn:
        def __init__(self):
            self.row_factory = None
        def execute(self, sql):
            calls["execs"].append(sql)
        def commit(self):
            calls["committed"] += 1
        def close(self):
            calls["closed"] += 1

    def fake_connect(path, timeout, detect_types, check_same_thread):
        calls["connect_args"] = {"path": path, "timeout": timeout, "detect_types": detect_types, "check_same_thread": check_same_thread}
        return FakeConn()

    monkeypatch.setattr(db_mod.sqlite3, "connect", fake_connect)

    with db_mod.get_db_connection(":memory:") as conn:
        assert conn is not None
        # Do a simple no-op
    # After context exit, commit and close should have been called
    assert calls["committed"] == 1
    assert calls["closed"] == 1
    # PRAGMA foreign_keys should have been executed
    assert any("foreign_keys" in (s or "").lower() for s in calls["execs"])
    # timeout should be default 10
    assert calls["connect_args"]["timeout"] == 10
    # check_same_thread should be False
    assert calls["connect_args"]["check_same_thread"] is False

def test_get_db_connection_rollbacks_on_exception(monkeypatch):
    calls = {"rolled_back": False, "closed": False}

    class FakeConn:
        def execute(self, sql):
            pass
        def rollback(self):
            calls["rolled_back"] = True
        def close(self):
            calls["closed"] = True

    def fake_connect(*args, **kwargs):
        return FakeConn()

    monkeypatch.setattr(db_mod.sqlite3, "connect", fake_connect)

    with pytest.raises(RuntimeError):
        with db_mod.get_db_connection(":memory:") as conn:
            raise RuntimeError("boom")

    assert calls["rolled_back"] is True
    assert calls["closed"] is True

def test_detect_types_and_timeout_passed(monkeypatch):
    observed = {}

    def fake_connect(path, timeout, detect_types, check_same_thread):
        observed["timeout"] = timeout
        observed["detect_types"] = detect_types
        observed["check_same_thread"] = check_same_thread
        class C:
            def __init__(self):
                self.row_factory = None
            def execute(self, sql):
                pass
            def commit(self):
                pass
            def close(self):
                pass
        return C()

    monkeypatch.setattr(db_mod.sqlite3, "connect", fake_connect)

    flags = sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
    with db_mod.get_db_connection(":memory:", timeout=7, detect_types=flags) as conn:
        pass

    assert observed["timeout"] == 7
    assert observed["detect_types"] == flags
    assert observed["check_same_thread"] is False

def test_prefers_current_app_database_path(monkeypatch):
    # Ensure when current_app.config['DATABASE_PATH'] exists it is used (passed into _resolve_db_path)
    recorded = {}

    # Create a fake current_app with config attribute
    fake_app = types.SimpleNamespace()
    fake_app.config = {"DATABASE_PATH": "sqlite:///fromapp.db"}

    monkeypatch.setattr(db_mod, "current_app", fake_app)

    def fake_resolve(path):
        recorded["resolved_input"] = path
        return ":memory:"

    monkeypatch.setattr(db_mod, "_resolve_db_path", fake_resolve)

    # monkeypatch sqlite3.connect to a no-op connection
    class NopConn:
        def __init__(self):
            self.row_factory = None
        def execute(self, sql):
            pass
        def commit(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(db_mod.sqlite3, "connect", lambda *a, **k: NopConn())

    with db_mod.get_db_connection(None) as conn:
        pass

    assert recorded.get("resolved_input") == "sqlite:///fromapp.db"