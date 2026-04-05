"""
app_core.utils.sql_param - small helper library to enforce parameterized SQL usage for sqlite3.

Provides:
 - execute_query(conn, query, params=()) -> sqlite3.Cursor
 - expand_in_clause_params(values) -> (placeholders_str, params_tuple)
 - count_placeholders(query) -> int
 - safe_sql_identifier(name: str) -> str
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional, Tuple, Sequence, Any

# Defensive import for sqlite3 typing/runtime use
try:
    import sqlite3
except Exception:
    sqlite3 = None  # type: ignore

# Use project logger when available
try:
    from app_core.app_logging import get_logger
    _logger = get_logger(__name__)
except Exception:
    _logger = logging.getLogger(__name__)


def _coerce_params(params: Optional[Sequence[Any]]) -> Tuple:
    """
    Ensure params is a tuple suitable for sqlite3.execute(..., params).
    If params is None -> empty tuple.
    If params is a single non-sequence scalar -> wrap into (params,).
    If params is list -> convert to tuple.
    """
    if params is None:
        return ()
    # If it's already a tuple, return
    if isinstance(params, tuple):
        return params
    # lists or other sequences -> convert
    if isinstance(params, list):
        return tuple(params)
    # If it's any other sequence (like generator) convert
    if isinstance(params, Sequence) and not isinstance(params, (str, bytes, bytearray)):
        try:
            return tuple(params)
        except Exception:
            pass
    # Fallback: treat as single scalar
    return (params,)


def count_placeholders(query: str) -> int:
    """
    Best-effort count of '?' placeholders in SQL string.

    Note: This is a simple heuristic that counts all occurrences of '?'.
    It may be inaccurate if '?' appears inside string literals in SQL, but it's only advisory.
    """
    try:
        return (query or "").count("?")
    except Exception:
        return 0


def expand_in_clause_params(values: Sequence[Any]) -> Tuple[str, Tuple]:
    """
    Build qmark-style placeholders and a params tuple for a variable-length IN clause.

    Example:
      placeholders, params = expand_in_clause_params([1,2,3])
      => placeholders == "?, ?, ?"   (commas + spaces)
         params == (1,2,3)

    Edge cases:
      - Empty values sequence -> returns ("NULL", ()) which can be used in queries such as:
          SELECT ... WHERE id IN (NULL)  -- yields no rows for typical use.
        Callers may prefer to short-circuit query when values empty.
    """
    if values is None:
        return ("NULL", ())
    # Convert to tuple for sqlite
    try:
        seq = tuple(values)
    except Exception:
        # If cannot convert, treat as single scalar
        seq = (values,)

    if len(seq) == 0:
        return ("NULL", ())

    placeholders = ", ".join("?" for _ in seq)
    return (placeholders, seq)


# -------------------------
# New: safe identifier helper
# -------------------------
def safe_sql_identifier(name: str) -> str:
    """
    Validate a SQL identifier (table/index/column name) for safe interpolation into SQL strings.

    Rules:
      - Must be a str
      - Only allow ASCII letters, digits and underscore
      - Length cap (default 255)
    Returns the validated name (unchanged) or raises ValueError.
    """
    if not isinstance(name, str):
        raise ValueError("Identifier must be a string")
    if name == "":
        raise ValueError("Identifier cannot be empty")
    if len(name) > 255:
        raise ValueError("Identifier too long")
    # Allow only [A-Za-z0-9_]
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError("Unsafe identifier characters detected")
    return name


def execute_query(conn, query: str, params: Optional[Sequence[Any]] = ()) -> "sqlite3.Cursor":
    """
    Execute a parameterized SQL query using sqlite3 style '?' placeholders.

    Parameters:
      - conn: sqlite3.Connection-like object exposing .execute(query, params)
      - query: SQL string (must be str)
      - params: tuple/list/scalar of parameters. If None -> empty tuple.

    Returns:
      - sqlite3.Cursor (whatever conn.execute returns)

    Behavior:
      - Validates input types and coerces params.
      - Logs debug/info about placeholder counts when mismatch is suspicious (advisory only).
      - On exception: logs and re-raises original exception.
    """
    if conn is None:
        raise TypeError("conn must be a DB-API connection instance (received None)")

    if not isinstance(query, str):
        raise TypeError("query must be a string")

    params_t = _coerce_params(params)

    # Advisory checks: log when param count doesn't match placeholder count (best-effort)
    try:
        ph_count = count_placeholders(query)
        if ph_count == 0 and len(params_t) > 0:
            try:
                _logger.warning("execute_query: query contains 0 '?' placeholders but params provided (advisory). Query may be using non-qmark binding or a mistake.", extra={"message_key": "sql.param_mismatch", "query_snip": query[:200], "params_len": len(params_t)})
            except Exception:
                _logger.warning("execute_query: placeholder mismatch (0 placeholders, params provided).")
        # If there are placeholders but params length is 0 -> warn
        if ph_count > 0 and len(params_t) == 0:
            try:
                _logger.debug("execute_query: query contains '?' placeholders but no params provided (advisory).", extra={"message_key": "sql.missing_params", "query_snip": query[:200], "placeholders": ph_count})
            except Exception:
                _logger.debug("execute_query: placeholders present but params is empty.")
    except Exception:
        # Non-fatal: proceed
        pass

    try:
        # Perform the DB-API execution; allow exception to bubble up after logging
        cur = conn.execute(query, params_t)
        try:
            _logger.debug("execute_query: executed SQL", extra={"message_key": "sql.executed", "query_snip": query[:200]})
        except Exception:
            pass
        return cur
    except Exception as e:
        # Log actionable message and re-raise
        try:
            _logger.exception("execute_query failed", extra={"message_key": "sql.execute_failed", "query_snip": (query[:400] if isinstance(query, str) else "<non-str>"), "params_len": len(params_t)})
        except Exception:
            _logger.error("execute_query failed: %s", str(e))
        raise


# Backwards-compatible exports
__all__ = ["execute_query", "expand_in_clause_params", "count_placeholders", "safe_sql_identifier"]
--- END FILE ---