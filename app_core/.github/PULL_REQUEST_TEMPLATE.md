<!-- Pull Request Checklist - KAN-124 CSRF Enforcement -->
Please use this checklist to ensure PRs meet repository guardrails before requesting review.

- [ ] I ran tests locally: pytest -q
- [ ] All new/changed templates or views that render <form> include CSRF protection:
      - an explicit hidden input: <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        OR
      - Flask-WTF hidden_tag() usage: form.hidden_tag() or hidden_tag()
- [ ] I ran the CSRF template audit test: pytest tests/test_csrf_templates.py
      - This test fails CI if any <form> in the code base is missing the CSRF marker
- [ ] No surgical module rewrites — only the files required for this Story were changed
- [ ] Trace file updated by tests: trace_KAN-124.txt (audit run logged)
- [ ] Security reviewer: please confirm CSRF controls are adequate for newly added forms

Developer guidance:
- If your PR introduces a new template string with a form, include the explicit hidden input above (use generate_csrf() to render token into the template when necessary).
- For forms rendered programmatically via form objects, ensure you call form.hidden_tag() in the form HTML.

Deployment notes:
- This test is primarily static and runtime rendered-check based and will run as part of the unit test suite. CI must include tests/ to gate merges.

RATIONALE & IMPLEMENTATION NOTES
- Why scan source files: This repo uses string-based HTML templates embedded in route modules (not file-based Jinja templates). The static scan prevents omission at source-level and is robust outside of full application rendering contexts.
- Why also render public pages: Some forms may be constructed dynamically; the render-time check ensures the token is actually rendered into responses.
- Remediation performed: analytics_link_view GET form did not include a hidden CSRF input. The surgical fix injects the explicit hidden input (csrf_token) into that form to satisfy the acceptance criteria and the audit test.
- Trace logging: tests/test_csrf_templates.py writes trace_KAN-124.txt so audit runs are recorded for the Architectural Memory requirement.
- Future-proofing: The test accepts either explicit input or hidden_tag(); if teams migrate to Flask-WTF form objects and use form.hidden_tag(), the audit still passes.

HOW TO APPLY THIS SURGICAL UPDATE
- Apply the single-line edit in routes/analytics.py (see content above).
- Add tests/test_csrf_templates.py file (content above).
- Add .github/PULL_REQUEST_TEMPLATE.md (content above).
- Run the unit tests:
    pytest -q
  The new tests will fail if any inline <form> block lacks CSRF presence.

Trace example (created by the test when run):
- trace_KAN-124.txt lines like:
  1623456789.123456 CSRF_AUDIT_PASSED static_scan
  1623456789.123789 CSRF_RENDER_PASSED runtime_check