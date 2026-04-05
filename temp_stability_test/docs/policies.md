# Input Sanitization & Templating Policies (KAN-180)

This document codifies the project's sanitization rules and best practices required by KAN-180.

1) Templates & Output Escaping
- Always render user-provided data via Jinja2 autoescape for HTML templates.
- Never apply the `|safe` filter to user-provided data. Any exception requires an explicit code comment and reviewer sign-off.
- When composing HTML via Python f-strings or string templates (inline rendering), call `html.escape()` on every user-provided value before insertion.

2) Server-side validation
- Email:
  - Validate syntactically (max length 255). Reject empty, control characters.
  - Store only hashed passwords; never log passwords.
- URL:
  - Use `app_core.utils.url_utils.normalize_url()` (preferred) or `validate_and_normalize_url()` wrapper.
  - Only allow `http` and `https` schemes.
  - By default reject private IP literals and hosts resolving to private addresses; configurable via app config.
  - Maximum URL length: 4096 chars.
- Short codes / slugs:
  - Two tiers:
    - Path-level strict validator: canonical regex `^[A-Za-z0-9]{1,16}$` (alphanumeric only).
    - User-provided custom slugs: validated by `utils.shortener.validate_custom_slug()` (allows `-` and `_`).
  - The redirect route behavior is controlled by `REDIRECT_ENFORCE_STRICT_SLUG` (bool). Default `False`.
  - When enabling strict enforcement, run migration/compatibility scan and remediate existing slugs.

3) Database Queries
- All SQL must use parameterized queries.
  - sqlite3: `conn.execute("SELECT ... WHERE x = ?", (val,))`
  - SQLAlchemy: use ORM filters or `text()` with bound params.
- Identifier interpolation (table/index/column names) is allowed only after validation via `safe_sql_identifier()`.
  - Rule: identifiers must match `^[A-Za-z0-9_]+$`, length <= 255.

4) Logging
- Log suspicious inputs at WARNING with message_key `security.suspicious_input`.
- Truncate values to avoid logging secrets (max snippet length 256) and include a short hashed suffix for correlation.
- Always include structured extras: `field`, `value_snip`, `route`, `request_id` when available.

5) Frontend & UX
- Client-side validation (HTML5) is recommended but cannot replace server-side validation.
- On validation failures, server re-renders forms preserving non-sensitive safe fields (email/name) only. Passwords must never be re-inserted.

6) CI & Tests
- New PRs touching templates or DB queries must include:
  - Unit tests verifying escaping for malicious payloads.
  - Static scan results showing no unsafe SQL interpolation.
  - No `|safe` usage on user data (grep enforced).
- CI includes:
  - `tools/check_sql_parameterization.py` scan
  - grep for `|safe`
  - Template audit `app_core/tools/html_audit.py` (extended checks)

7) Rollout Recommendations
- When enabling strict slug enforcement, set `REDIRECT_ENFORCE_STRICT_SLUG=False` first.
- Run compatibility scans to identify slugs that would be rejected.
- Remediate or map legacy slugs before flipping to `True`.

8) Exceptions & Review
- Any exception to these rules must be explicitly documented in the PR and approved by a security reviewer.

# End of policies.md
--- END FILE ---