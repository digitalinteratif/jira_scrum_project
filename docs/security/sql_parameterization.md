# SQL Parameterization Guide (KAN-164)

This document explains the project's lightweight helper and guidelines to prevent SQL injection
when using sqlite3/raw SQL. Use the helper functions in `app_core.utils.sql_param` and run the
checker `tools/check_sql_parameterization.py` as part of CI and local pre-commit to avoid regressions.

## Key principles

- Never interpolate user input into SQL strings via f-strings, string concatenation, `.format()` or `%` formatting.
- For sqlite3 use qmark placeholders (`?`) and pass parameters as a tuple to the DB-API execute call.
- For SQL that requires a variable-length IN(...) list, use `expand_in_clause_params()` to safely create placeholders and params.
- When using SQLAlchemy, prefer `text()` bound parameters or ORM APIs (named binds like `:name`).

## API

Import:

```python
from app_core.utils.sql_param import execute_query, expand_in_clause_params
```

Examples:

- INSERT:

```python
execute_query(conn, "INSERT INTO users(email, password_hash) VALUES(?, ?)", (email, password_hash))
```

- SELECT:

```python
cur = execute_query(conn, "SELECT id, email FROM users WHERE email = ?", (email,))
row = cur.fetchone()
```

- IN clause:

```python
placeholders, params = expand_in_clause_params([1, 2, 3])
sql = f"SELECT id, title FROM posts WHERE id IN ({placeholders})"
cur = execute_query(conn, sql, params)
```

- Safe insertion of data containing `?` characters:

```python
url = "https://example.com/?q=who?"
execute_query(conn, "INSERT INTO urls(original_url) VALUES(?)", (url,))
```

## Bad patterns (do not use)

- f-strings:
  ```python
  cur.execute(f"SELECT * FROM users WHERE email = '{email}'")
  ```

- Concatenation:
  ```python
  q = "SELECT * FROM users WHERE id = " + user_id
  cur.execute(q)
  ```

- `.format()`:
  ```python
  cur.execute("SELECT * FROM users WHERE email = '{}'".format(email))
  ```

## Lint/check usage

Run the checker locally:

```bash
python tools/check_sql_parameterization.py
```

In CI the command is run over repository files; the job fails if potential unsafe patterns are found.

If a flagged instance is intentional and safe, suppress it inline with:

```python
# noqa:sql-param
```

and include a SURGICAL_RATIONALE in your PR description explaining why it's safe and unavoidable.

## Tests

Unit and integration tests exist under `app_core/tests/` to verify:
- execute_query inserts/selects correctly
- strings with SQL meta-characters are stored literally
- '?' characters in data are preserved
- expand_in_clause_params builds correct placeholders

## Reviewer checklist (add to PR template)
- [ ] All raw SQL uses parameterized queries (no f-strings/concatenation).
- [ ] Ran `tools/check_sql_parameterization.py` and fixed findings (or added inline suppression and SURGICAL_RATIONALE).
- [ ] Added/updated tests that cover new SQL paths.
--- END FILE ---