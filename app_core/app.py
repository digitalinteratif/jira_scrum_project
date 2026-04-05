@@
     # --- 6. DATABASE INITIALIZATION ---
@@
         try:
             if project_logger:
                 project_logger.info("Database initialized", extra={"message_key": "database.initialized", "db_path": db.engine.url.__to_string__() if hasattr(db, 'engine') else app.config.get("SQLALCHEMY_DATABASE_URI")})
             else:
                 app.logger.info("Database initialized")
         except Exception:
             pass
     except Exception as e:
         # Database initialization errors are fatal during app bootstrap; surface them.
         try:
             if project_logger:
                 project_logger.error("Database creation failed during app startup.", extra={"message_key": "db.creation_failed", "trace": traceback.format_exc()})
             else:
                 app.logger.error("Database creation failed during app startup.\n%s", traceback.format_exc())
         except Exception:
             pass
         raise
+
+    # --- 7. REGISTER REDIRECT ROUTE (KAN-176) ---
+    # This must be registered after all explicit blueprints so specific routes win before the catch-all slug.
+    try:
+        # Import the view function from our redirector module and register a root-level rule.
+        # We register as endpoint "shortener.redirect_slug" so existing url_for usages continue to work.
+        try:
+            from app_core.routes.redirector import redirect_short_code
+        except Exception:
+            # Surface import errors as fatal so CI/dev sees the root cause early.
+            raise
+
+        # Add URL rule at root-level for single-segment slugs: '/<short_code>'
+        # Use endpoint name expected by other modules for compatibility.
+        try:
+            app.add_url_rule("/<string:short_code>", endpoint="shortener.redirect_slug", view_func=redirect_short_code, methods=["GET"])
+            try:
+                if project_logger:
+                    project_logger.info("Redirect route registered.", extra={"message_key": "app.redirect_registered", "endpoint": "shortener.redirect_slug"})
+                else:
+                    app.logger.info("SUCCESS: Redirect route registered as shortener.redirect_slug")
+            except Exception:
+                pass
+        except Exception as e:
+            # If adding the rule fails, log and re-raise to fail-fast
+            try:
+                if project_logger:
+                    project_logger.error("CRITICAL FAIL: Redirect route failed to register.", extra={"message_key": "app.redirect_register_failed", "trace": traceback.format_exc()})
+                else:
+                    app.logger.error("CRITICAL FAIL: Redirect route failed to register.\n%s", traceback.format_exc())
+            except Exception:
+                pass
+            raise
+    except Exception:
+        # Fail-fast: do not swallow registration errors
+        raise
+
+    # --- 8. REGISTER CENTRALIZED ERROR HANDLERS (KAN-181) ---
+    try:
+        # Import and register our error handlers after blueprints/routes are in place so specific handlers take precedence.
+        try:
+            from app_core.error_handlers import register_error_handlers
+        except Exception:
+            # Surface import errors so CI/dev sees failures early
+            raise
+
+        try:
+            register_error_handlers(app)
+            try:
+                if project_logger:
+                    project_logger.info("Error handlers registered.", extra={"message_key": "app.error_handlers_registered"})
+                else:
+                    app.logger.info("SUCCESS: Error handlers registered")
+            except Exception:
+                pass
+        except Exception:
+            try:
+                if project_logger:
+                    project_logger.error("Failed to register error handlers during app startup.", extra={"message_key": "app.error_handlers_register_failed", "trace": traceback.format_exc()})
+                else:
+                    app.logger.error("Failed to register error handlers during app startup.\n%s", traceback.format_exc())
+            except Exception:
+                pass
+            # Fail-fast: allow the exception to surface so CI shows the problem
+            raise
+    except Exception:
+        # Re-raise to fail app creation; handlers are critical for user-facing behavior
+        raise
@@
     return app
*** NOTE: the Gunicorn 'app' variable is created after create_app()
*** and remains unchanged below.
*** CORRECT PLACEMENT ***
*** (rest of file unchanged) ***