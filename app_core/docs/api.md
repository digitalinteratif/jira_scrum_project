# API: Programmatic URL Shortener (KAN-145)

This document describes the programmatic API for creating short URLs.

Base URL
- POST /api/shorten

Authentication
- API Key required.
- Provide the API key via either:
  - Authorization: Bearer <API_KEY>
  - X-API-Key: <API_KEY>
  - Query or body parameter `api_key` (not recommended for production)
- The API key is scoped to a user (owner) and operations performed will be created under that owner's account.

Request
- Content-Type: application/json
- Body JSON fields:
  - target_url (string, required) — absolute URL to shorten (http/https).
  - slug (string, optional) — user-provided custom slug (when provided and valid).
  - is_custom (bool, optional) — whether provided slug is custom (default false if not provided).
  - expire_at (string, optional) — ISO-8601 or "YYYY-MM-DD HH:MM" UTC format to set expiration.
  - deterministic (bool, optional) — request deterministic slug generation from target+secret (best-effort).

Example:
```
curl -X POST https://digitalinteractif.com/api/shorten \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "target_url": "https://example.com/path?query=1",
    "slug": "optional-slug",
    "is_custom": true
  }'
```

Response
- 201 Created (application/json) on success:
  ```json
  {
    "id": 123,
    "slug": "abcd1234",
    "short_url": "https://digitalinteractif.com/abcd1234",
    "target_url": "https://example.com/path?query=1"
  }
  ```

- 400 Bad Request (JSON) on invalid input or duplicate slug:
  ```json
  {
    "error": "slug_conflict",
    "slug": "abcd",
    "suggestions": ["abcd-1","abcd-x"]
  }
  ```

- 401 Unauthorized when missing/invalid key:
  ```json
  { "error": "missing_api_key" }
  ```
  or
  ```json
  { "error": "invalid_api_key" }
  ```

- 403 Forbidden when key revoked:
  ```json
  { "error": "api_key_revoked" }
  ```

- 429 Too Many Requests when key exceeded per-key rate limit. Response includes these headers:
  - X-RateLimit-Limit: integer
  - X-RateLimit-Remaining: integer
  - X-RateLimit-Reset: epoch seconds (UTC) at which bucket will be refilled

Rate limiting
- Each API key carries rate-limit settings; the server will respond with standard headers described above.
- Default server-wide fallback values:
  - API_DEFAULT_RATE_LIMIT_CAPACITY (app.config): default capacity if API key has no specific settings (default 60)
  - API_DEFAULT_RATE_LIMIT_WINDOW_SECONDS (app.config): default window (default 60)

Operational notes
- The API reuses internal creation logic (models.create_shorturl) to ensure consistent slug uniqueness behavior.
- API key operations are scoped to the key's owner — the ID Filter rule is enforced server-side.
- All interactions are logged to trace_KAN-145.txt for Architectural Memory (best-effort).
- For large-scale programmatic usage request an operator-issued API key with custom rate limits.

Testing guidance
- Unit tests:
  - test decorator: call a dummy endpoint with and without API keys; assert 401/403/200 behaviors and that g.api_user_id is set.
  - test rate-limit: configure API key with low capacity/window and assert 429 after exceeding.
- Integration tests:
  - create an APIKey in DB for a test user (db fixture).
  - POST /api/shorten with valid payload and API key; assert 201 and DB ShortURL created owned by that user.
  - POST repeatedly beyond per-key capacity; assert 429 and rate-limit headers present.
  - Attempt to create a slug for another user via API key (should create under the API key's owner, not the provided user_id).

Migration note
- models.py changed (new api_keys table). Add a simple migration or use models.Base.metadata.create_all(engine) during early dev/testing to create the table.
- Suggested alembic migration (surgical) to create the `api_keys` table with the fields in the model and an index on `key`.

Operator / config knobs (optional)
- Add to environment / app config:
  - API_DEFAULT_RATE_LIMIT_CAPACITY (int) default 60
  - API_DEFAULT_RATE_LIMIT_WINDOW_SECONDS (int) default 60
These are referenced in the code as fallbacks; operators may set them in production as needed.

Trace / Architectural Memory
- routes/api.py writes to trace_KAN-145.txt on auth, rate-limit hit, creation success/failure so cross-agent interactions are recorded per the project rules.
--- END FILE ---