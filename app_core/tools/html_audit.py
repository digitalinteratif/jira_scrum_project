#!/usr/bin/env python3
"""
Basic HTML/Tailwind structural audit for string-based rendering.
Checks for:
- forms missing CSRF token
- viewport meta tag presence
- forbidden jinja block/extends usage
- simple mismatched tag counts (heuristic)
Outputs JSON mapping file -> list of issues.
"""
import os
import re
import json

ROOT = "app_core"
issues = {}

def find_forms(s):
    return re.findall(r"<form[^>]*>.*?</form>", s, flags=re.S | re.I)

for root, dirs, files in os.walk(ROOT):
    for fn in files:
        if fn.endswith(".py") or fn.endswith(".html"):
            path = os.path.join(root, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    src = f.read()
            except Exception as e:
                issues.setdefault(path, []).append(f"read_error: {e}")
                continue
            # Only audit files that reference render_layout or look like template modules
            if "render_layout(" not in src and "render_template(" not in src and "<form" not in src:
                continue
            s = src
            # forms: check for csrf token presence
            forms = find_forms(s)
            for form in forms:
                if 'name="csrf_token"' not in form and "csrf_token()" not in form and "csrf_token' not in form":
                    issues.setdefault(path, []).append("form missing csrf_token (POST forms must include hidden csrf_token)")
            # viewport meta
            if "<meta name=\"viewport\"" not in s and "<meta name='viewport'" not in s:
                issues.setdefault(path, []).append("viewport meta missing")
            # forbidden jinja tags
            if "{% extends" in s or "{% block" in s:
                issues.setdefault(path, []).append("forbidden jinja extend/block usage found")
            # tailwind class heuristic: flag obvious typos (double colons, non-alpha sequences)
            bad_classes = re.findall(r'class=["\']([^"\']+)["\']', s)
            for cls in bad_classes:
                for token in cls.split():
                    if "::" in token or ";" in token:
                        issues.setdefault(path, []).append(f"possibly invalid tailwind class token: {token}")
            # tag count mismatch heuristic
            for tag in ["div","p","form","header","footer","main","section","a","ul","li"]:
                open_count = len(re.findall(fr"<{tag}(\s|>)", s, flags=re.I))
                close_count = len(re.findall(fr"</{tag}>", s, flags=re.I))
                if open_count != close_count:
                    issues.setdefault(path, []).append(f"mismatched <{tag}>: opens={open_count} closes={close_count}")
            # accessibility checks
            if "<img" in s and "alt=" not in s:
                issues.setdefault(path, []).append("img tag(s) without alt attribute")
            if "<button" in s and "aria-" not in s and "aria" not in s:
                # not all buttons require aria but flag for review
                issues.setdefault(path, []).append("button tags present; verify aria-labels for accessibility")
print(json.dumps(issues, indent=2))