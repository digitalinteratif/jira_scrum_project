## Summary of changes

Please provide a short description of the change and the files touched.

## Security checklist (required)

- [ ] I verified no plaintext passwords are stored (registration / create_user paths validated).
- [ ] All DB queries in my changes use parameterized bindings (no f-strings or .format() for SQL).
- [ ] No empty or catch-all except blocks introduced (e.g., `except Exception: pass`).
- [ ] Any session or JWT cookies set include HttpOnly and SameSite; Secure set for production deployments.
- [ ] Inputs (URLs, slugs, form fields) are validated via `validate_and_normalize_url` / `validate_short_code` and templates escape user data.
- [ ] I ran: `ruff check .`, `pytest -q`, and `scripts/security_scan.sh` locally — all passed.
- [ ] I considered logging and did not add logs that include secrets (passwords, raw tokens).

## Testing notes

Describe manual or automated steps you ran to validate the change (e.g., curl to inspect Set-Cookie headers, unit tests added, etc).

## Reviewer guidance

List files where security-sensitive changes occurred that require careful review (e.g., auth, DB access, cookie handling).