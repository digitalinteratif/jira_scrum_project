"""Tests for expand_in_clause_params integration with execute_query."""
import pytest
from app_core.utils import sql_param as sp
from app_core import db as db_mod

def test_expand_in_clause_and_query():
    with db_mod.get_db_connection(":memory:") as conn:
        sp.execute_query(conn, "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);", ())
        for i in range(1, 6):
            sp.execute_query(conn, "INSERT INTO items(id, name) VALUES(?, ?)", (i, f"item-{i}"))
        ids = [2,4]
        placeholders, params = sp.expand_in_clause_params(ids)
        sql = f"SELECT id, name FROM items WHERE id IN ({placeholders}) ORDER BY id"
        cur = sp.execute_query(conn, sql, params)
        rows = cur.fetchall()
        assert [r["id"] for r in rows] == [2,4]
--- END FILE ---