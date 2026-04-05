"""Unit tests for app_core.utils.sql_param helpers."""
import pytest

from app_core.utils import sql_param as sp
from app_core import db as db_mod

def test_expand_in_clause_params_basic():
    placeholders, params = sp.expand_in_clause_params([1,2,3])
    assert placeholders.count("?") == 3
    assert isinstance(params, tuple)
    assert params == (1,2,3)

def test_count_placeholders():
    q = "SELECT * FROM t WHERE a = ? AND b = ?"
    assert sp.count_placeholders(q) == 2

def test_execute_query_insert_and_select():
    with db_mod.get_db_connection(":memory:") as conn:
        # create table
        sp.execute_query(conn, "CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT);", ())
        # insert
        cur = sp.execute_query(conn, "INSERT INTO test(val) VALUES(?)", ("hello",))
        # cursor should have lastrowid
        assert hasattr(cur, "lastrowid")
        # select
        cur2 = sp.execute_query(conn, "SELECT id, val FROM test WHERE val = ?", ("hello",))
        rows = cur2.fetchall()
        assert len(rows) == 1
        assert rows[0]["val"] == "hello"
--- END FILE ---