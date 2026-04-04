"""utils/templates.py - render_layout helper (accessibility & responsive polish for KAN-143)"""

def render_layout(inner_html: str):
    """
    Wrap content in a consistent header/footer and minimal accessibility-focused CSS + skip link.

    Notes:
      - Do not use Jinja extends/blocks; return full HTML string (project requirement).
      - This wrapper includes:
          * meta viewport for responsive layouts
          * "Skip to content" link as first interactive element
          * styles for visible focus outlines and accessible colors
          * an announcer region (aria-live) for ephemeral messages (copy feedback)
      - Best-effort trace written to trace_KAN-143.txt for Architectural Memory.
    """
    try:
        # Best-effort trace write (non-blocking)
        with open("trace_KAN-143.txt", "a") as f:
            import time
            f.write(f"{time.time():.6f} render_layout_called\n")
    except Exception:
        # do not raise on trace failures
        pass

    # Minimal accessible/responsive CSS + skip link
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1"> <!-- responsive view -->
  <title>Smart Link</title>
  <style>
    /* Basic layout */
    body {{ font-family: Arial, sans-serif; margin: 0; padding: 0; background: #fff; color: #111; }}
    .site-wrapper {{ max-width: 960px; margin: 0 auto; padding: 1rem; }}
    header, footer {{ padding: 1rem 0; }}

    /* Skip link - visually hidden until focused */
    .skip-link {{
      position: absolute;
      left: -999px;
      top: auto;
      width: 1px;
      height: 1px;
      overflow: hidden;
      z-index: 1000;
    }}
    .skip-link:focus, .skip-link:active {{
      left: 1rem;
      top: 1rem;
      width: auto;
      height: auto;
      padding: 0.5rem 1rem;
      background: #000;
      color: #fff;
      text-decoration: none;
      border-radius: 4px;
    }}

    /* Accessible focus outlines */
    a:focus, button:focus, input:focus, textarea:focus {{
      outline: 3px solid #005fcc;
      outline-offset: 2px;
    }}

    /* Forms */
    label {{ display: block; margin: 0.5rem 0 0.25rem; font-weight: bold; }}
    input, textarea, select {{ padding: 0.5rem; font-size: 1rem; width: 100%; box-sizing: border-box; }}
    button {{ padding: 0.5rem 1rem; font-size: 1rem; cursor: pointer; }}

    /* Make tables responsive on small screens */
    .responsive-table {{ width: 100%; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 0.5rem; text-align: left; border-bottom: 1px solid #e0e0e0; }}

    /* Accessible copy control */
    .shortlink-row {{ display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }}
    .shortlink-input {{ flex: 1 1 200px; min-width: 150px; }}
    .copy-btn {{ flex: 0 0 auto; }}

    /* Make content readable on small viewports */
    @media (max-width: 600px) {{
      .site-wrapper {{ padding: 0.75rem; }}
      header h1 {{ font-size: 1.25rem; }}
    }}
  </style>
</head>
<body>
  <!-- Skip link for keyboard users -->
  <a class="skip-link" href="#main-content">Skip to content</a>

  <div class="site-wrapper">
    <header>
      <h1><a href="/" style="color:inherit; text-decoration:none;">Smart Link</a></h1>
    </header>

    <main id="main-content" tabindex="-1">
      {inner_html}
      <!-- aria-live region for ephemeral UI messages (e.g., copy-to-clipboard feedback) -->
      <div id="a11y-announcer" aria-live="polite" style="position: absolute; left: -9999px; height: 1px; overflow: hidden;"></div>
    </main>

    <footer>
      <p>© Smart Link</p>
    </footer>
  </div>

  <script>
    // Lightweight helper available to pages to announce messages to screen readers
    window.__smartlinkAnnounce = function(msg) {{
      try {{
        var ann = document.getElementById('a11y-announcer');
        if (!ann) return;
        ann.textContent = '';
        // Force a DOM update before setting message to ensure assistive tech picks it up
        setTimeout(function() {{ ann.textContent = msg; }}, 50);
      }} catch (e) {{
        // no-op
      }}
    }};
  </script>
</body>
</html>"""
# --- END FILE: utils/templates.py ---