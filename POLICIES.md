Repository-wide Logging & Error-Handling Policy (KAN-159)
Version: 1.0
Author: Engineering

Purpose
- Ensure runtime events are visible in logs and to avoid silent failures caused by empty except blocks or swallowed exceptions.

Policy statements
1) No empty except blocks
   - Forbidden:
       except:
           pass
       except Exception:
           pass
       except ValueError:
           # nothing
   - Rationale: They hide failures and make RCA impossible.
   - Enforcement: CI and pre-commit checks will block changes introducing empty except bodies.

2) Always log exceptions with context
   - For unexpected errors: use logger.exception("Human readable message", extra={ "message_key": "...", ... })
     This records the full stack trace.
   - For expected validation errors or known conditions: use logger.warning or logger.info with message_key and relevant structured fields. Do not use logger.exception unless the stack trace is helpful.
   - When using try/except to convert exceptions into user-facing errors, always log the original exception with logger.exception at an appropriate level (ERROR/WARNING) unless there is a clear reason not to (document reason in code comments and tests).

3) Use structured logs and message_key taxonomy
   - Use the message_key naming convention: <area>.<action> (e.g., auth.login_failed).
   - Include contextual structured fields in extra= dict (e.g., user_id, db_path, slug).
   - Do not include sensitive data in logs (passwords, full auth tokens). Mask PII and secrets.

4) Use app_core.app_logging.logger
   - Import the repository logger: from app_core.app_logging import logger.
   - Avoid direct use of print() for production logs.

5) Exception sanitization
   - If logging errors that include user data, sanitize or mask personal data fields before logging.

6) Developer notes
   - If you must temporarily swallow an exception for highly defensive code paths, add a one-line TODO with owner and JIRA reference and ensure that the exception is at least logged (INFO or DEBUG). This pattern is strongly discouraged.

Enforcement
- A pre-commit hook will run a static checker to find empty except blocks and block commits.
- CI pipeline will run the same check and fail builds that contain empty except blocks.
- PR reviewers must ensure changes adhere to this policy; PR template contains a checklist item for "No empty except blocks".

Exception handling guidelines (concrete)
- Use logger.exception for full stack trace.
- Use logger.error("message", extra={"message_key":"...", "something": value}) when you need to attach the exception context but not full traceback (you can pass exc_info=True).
- For expected recoverable errors, use logger.warning with message_key and user-facing error message.
- Always return or re-raise after logging if the exception needs to be surfaced. Do not swallow.
--- END FILE ---