"""app_core.utils.errors - ValidationError for standardized auth/form validation handling (KAN-171)"""

from __future__ import annotations
from typing import Optional, Dict, Any


class ValidationError(Exception):
    """
    Exception representing a user-facing validation failure.

    Attributes:
      - message: human-readable message safe to display in templates
      - status_code: HTTP status code to return (4xx)
      - field: optional field name associated with the error (e.g., "email")
      - extra: mapping for additional structured info (for handler/logging). Should NOT contain sensitive values.
    """

    def __init__(self, message: str, status_code: int = 400, field: Optional[str] = None, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = str(message)
        try:
            self.status_code = int(status_code)
        except Exception:
            self.status_code = 400
        self.field = field
        self.extra = dict(extra or {})

    def to_dict(self):
        return {"message": self.message, "status_code": self.status_code, "field": self.field, "extra": self.extra}
--- END FILE ---