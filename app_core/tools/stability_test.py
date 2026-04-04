#!/usr/bin/env python3
"""
10-second stability test: import create_app from app.py and exercise endpoints using Flask test_client.
Exit codes:
0 - pass
2 - import error
3 - stability test failed
"""
import time
import sys

try:
    from app import create_app  # app.py must expose create_app factory
except Exception as e:
    print("IMPORT_ERROR:", e)
    sys.exit(2)

try:
    app = create_app()
except Exception as e:
    print("APP_FACTORY_ERROR:", e)
    sys.exit(2)

client = app.test_client()

endpoints = ["/", "/login", "/register", "/shorten"]
start = time.time()
errors = []
while time.time() - start < 10:
    for ep in endpoints:
        try:
            r = client.get(ep)
            if r.status_code >= 500:
                errors.append((ep, r.status_code))
        except Exception as ee:
            errors.append((ep, str(ee)))
    time.sleep(0.5)

if errors:
    print("STABILITY_TEST_FAIL", errors)
    sys.exit(3)
else:
    print("STABILITY_TEST_PASS")
    sys.exit(0)