# package marker for app_core.routes
# Created to ensure package imports succeed in production (KAN-150 remediation)
# No executable code here. This file prevents ModuleNotFoundError when importing
# modules like app_core.routes.shortener in WSGI/gunicorn environments.