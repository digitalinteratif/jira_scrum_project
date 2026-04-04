Contributing / Developer Setup — formatting, linting and pre-commit hooks
========================================================================

This project enforces consistent formatting, linting, import sorting and (optionally) dependency security checks.
We use pre-commit + black + ruff + isort + flake8 to keep changes small and reviewable.
CI enforces the same checks and will fail PRs with violations.

Quick start (recommended)
-------------------------
1) Create and activate your virtualenv:
   python -m venv .venv
   source .venv/bin/activate    # on macOS/Linux
   .venv\Scripts\activate       # on Windows (PowerShell: .venv\Scripts\Activate.ps1)

2) Install tooling (no project dependencies required):
   pip install --upgrade pip
   pip install pre-commit black ruff isort flake8

3) Install git pre-commit hooks:
   pre-commit install
   # Optionally verify right away:
   pre-commit run --all-files

4) Before opening a PR:
   - Run `pre-commit run --all-files` to auto-fix where possible (black, isort, ruff --fix)
   - If local auto-fixes are applied, re-stage the changed files and commit.

Design notes
------------
- Black is the canonical formatter. isort is configured to match Black style.
- Ruff is the primary linter and will auto-fix many issues. flake8 is kept for complementary checks.
- The pyproject.toml contains project-specific configurations for Black/isort/ruff/flake8.
- Hooks in .pre-commit-config.yaml are pinned to specific revisions to ensure reproducible behavior.

CI integration
--------------
- CI runs pre-commit across the entire tree (`pre-commit run --all-files`) so that violations can't be pushed/merged.
- CI also runs ruff, isort (check-only), black (check-only) and flake8 to provide explicit error output.
- Optional safety checks run in CI to catch known vulnerable packages (SAFETY_ENABLED environment / secret controls).

Performance / large diffs
-------------------------
Pre-commit is intentionally fast, but very large diffs may be slow. Recommended approaches:
- Run pre-commit only on changed files:
    pre-commit run --hook-stage commit
  or
    pre-commit run <hook-id> --files path/to/file1.py path/to/file2.py
- Run formatters with parallel/targeted invocations:
    black path/to/subtree
    isort path/to/subtree
    ruff check path/to/subtree --fix
- For massive refactors where automated formatting is noisy, run the formatters locally once and commit the result in a dedicated formatting-only PR to make reviewable subsequent changes easier.

Bypassing hooks
----------------
- You can skip client-side hooks with `git commit --no-verify`, but CI will still run checks and reject PRs that fail them. Do NOT rely on `--no-verify` to bypass checks for code that will be merged.

Troubleshooting
---------------
- If pre-commit reports an error about hook versions, ensure you have the pinned versions installed or run:
    pip install --upgrade pre-commit
    pre-commit autoupdate
  Note: autoupdate will change pinned revs; commit any changes to .pre-commit-config.yaml only with care and follow project policy.

Contact / Exceptions
--------------------
- If you need to change linting rules (e.g., allow a specific style exception), open a PR describing the rationale. Small, surgical rule changes are preferred.
- For CI configuration changes (e.g., disabling safety due to offline CI), coordinate with the ops team and update the CI YAML accordingly.
--- END FILE ---