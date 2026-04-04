#!/usr/bin/env python3
"""
Scan app_core/ for blueprint definitions, decorator usage, imports of render_layout,
and presence of __init__.py files. Produces JSON to help RCA.
"""
import ast
import json
import os
import sys

ROOT = os.path.join(os.getcwd(), "app_core")
out = {"files": {}}

def scan_file(path):
    data = {
        "blueprint_defs": [],
        "blueprint_decorators": [],
        "imports": [],
        "has_render_layout_import": False,
        "has_init": os.path.basename(path) == "__init__.py",
        "parse_error": None,
        "error": None,
    }
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception as e:
        data["error"] = str(e)
        return data
    try:
        tree = ast.parse(src, filename=path)
    except Exception as e:
        data["parse_error"] = str(e)
        return data
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    # heuristic: Blueprint(...) assigned
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        if isinstance(func, ast.Name) and func.id == "Blueprint":
                            data["blueprint_defs"].append(target.id)
                        # support flask.Blueprint
                        if isinstance(func, ast.Attribute) and getattr(func, "attr", "") == "Blueprint":
                            data["blueprint_defs"].append(target.id)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                func = deco.func if isinstance(deco, ast.Call) else deco
                # decorator like shortener_bp.route
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    if func.attr == "route":
                        data["blueprint_decorators"].append({"decorator_obj": func.value.id, "lineno": getattr(deco, "lineno", None)})
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for n in node.names:
                if n.name == "render_layout" and mod.startswith("app_core.routes.home"):
                    data["has_render_layout_import"] = True
        if isinstance(node, ast.Import):
            for n in node.names:
                data["imports"].append(n.name)
    return data

for root, dirs, files in os.walk(ROOT):
    for fn in files:
        if fn.endswith(".py"):
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, ROOT)
            out["files"][rel] = scan_file(path)

print(json.dumps(out, indent=2))