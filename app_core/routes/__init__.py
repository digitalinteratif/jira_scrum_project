# Package marker for app_core.routes (KAN-154 surgical fix).
# Keeps package available for imports like `from app_core.routes.auth import auth_bp`.
# Non-blocking trace to assist future RCA.
try:
    with open("trace_KAN-154.txt", "a") as _tf:
        import time
        _tf.write(f"{time.time():.6f} routes.__init__ loaded\n")
except Exception:
    pass