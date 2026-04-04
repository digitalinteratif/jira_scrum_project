"""
routes/health.py - Minimal healthcheck blueprint (KAN-150 surgical addition)

Provides:
  - GET /health -> returns plain-text "ok"
Rationale:
  - CI smoke runner expects /health to return 'ok' to consider the app alive.
  - This blueprint is intentionally tiny and dependency-light so it can be registered early.
"""

from flask import Blueprint, Response

health_bp = Blueprint("health", __name__)

@health_bp.route("/health", methods=["GET"])
def health():
    # Plain "ok" body with 200 status. Keep response minimal and dependency-free.
    return Response("ok", mimetype="text/plain; charset=utf-8")