"""ast-based helper validation.

Pure parser-level checks. Returns a fail reason string or None.
Loader wraps the reason into HelperLoadError downstream — validator never
raises (syntax errors come back as a reason too, so all "cannot load"
reasons share one channel).

Check order: syntax -> imports -> docstrings. Only the first violation is
returned; agent rewrites and we re-validate.
"""

from __future__ import annotations

import ast

_FORBIDDEN_BUILTINS = frozenset({"__import__", "exec", "eval"})


def check(source: str, service: str) -> str | None:
    """Validate helper source by ast. Returns reason on fail, None on pass.

    `service` is the current helper module name (e.g. "github") used to
    permit self-references and reject cross-service imports.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"syntax error: {exc.msg}"

    if reason := _check_imports(tree, service):
        return reason
    return _check_docstrings(tree)


def _check_imports(tree: ast.Module, service: str) -> str | None:
    allowed = f"helpers.{service}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "importlib" or name.startswith("importlib."):
                    return "forbidden dynamic import: importlib"
                if name.startswith("helpers.") and name != allowed:
                    return f"forbidden cross-service import: {name} (current service: {service})"
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                return f"forbidden relative import (level={node.level})"
            module = node.module or ""
            if module == "importlib" or module.startswith("importlib."):
                return "forbidden dynamic import: importlib"
            if module.startswith("helpers.") and module != allowed:
                return f"forbidden cross-service import: {module} (current service: {service})"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fname = node.func.id
            if fname in _FORBIDDEN_BUILTINS:
                return f"forbidden builtin: {fname}"
    return None


def _check_docstrings(tree: ast.Module) -> str | None:
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name.startswith("_"):
            continue
        doc = ast.get_docstring(node)
        if doc is None:
            return f"function '{node.name}' missing docstring (require 'Generalization:' section)"
        if "Generalization:" not in doc:
            return f"function '{node.name}' missing 'Generalization:' in docstring"
    return None
