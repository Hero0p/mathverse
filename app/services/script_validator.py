"""Static AST allowlist for generated Manim scripts.

This runs BEFORE anything is ever handed to the sandbox container. It is a
defense-in-depth layer, not the only one -- the sandbox itself also drops
network access, mounts a read-only filesystem, and runs as a non-root user
with strict resource limits (see sandbox/runner.py). Nothing here should be
treated as sufficient on its own.
"""
import ast
import re

ALLOWED_IMPORTS = {
    "manim": None,  # None means "any name imported from this module is fine"
    "numpy": {"np"},
    "math": None,
}

# Modules/names that must never appear, even nested inside an allowed import
# path or as an attribute access, because they grant filesystem/process/net access.
FORBIDDEN_NAMES = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib",
    "ctypes", "multiprocessing", "threading", "signal", "resource", "pty",
    "open", "eval", "exec", "compile", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "requests", "urllib", "http", "ftplib", "smtplib",
}

FORBIDDEN_DUNDER_PREFIXES = ("__class__", "__base__", "__subclasses__", "__globals__", "__builtins__")


class ScriptValidationError(ValueError):
    def __init__(self, message: str, lineno: int | None = None):
        super().__init__(message)
        self.lineno = lineno


# These have to be narrow, because Manim scripts are full of LaTeX and LaTeX
# is full of backslashes. In particular "\\\\" in Python source is the LaTeX
# line break `\\`, which appears in a large share of legitimate MathTex --
# a blanket "starts with two backslashes" rule rejects valid math, so UNC
# detection requires an actual \\server\ shape.
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_PATH_RE = re.compile(r"^\\\\[A-Za-z0-9._-]+\\")
_SYSTEM_PATH_RE = re.compile(
    r"^/(etc|usr|bin|sbin|home|root|var|proc|sys|dev|boot|opt|tmp)(/|$)"
)


def _looks_like_filesystem_path(value: str) -> bool:
    """Whether a string literal is plausibly a real path.

    Defense in depth only: the import and call allowlists already remove the
    means to *use* a path (no os, pathlib, open, subprocess). This just
    catches an obvious attempt early with a clearer message.
    """
    return bool(
        _DRIVE_PATH_RE.match(value)
        or _UNC_PATH_RE.match(value)
        or _SYSTEM_PATH_RE.match(value)
    )


def _check_import(node) -> list[str]:
    errors = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_IMPORTS:
                errors.append(f"line {node.lineno}: import of '{alias.name}' is not allowed")
    elif isinstance(node, ast.ImportFrom):
        module = (node.module or "").split(".")[0]
        if module not in ALLOWED_IMPORTS:
            errors.append(f"line {node.lineno}: import from '{node.module}' is not allowed")
    return errors


def _check_name(node, errors: list[str]) -> None:
    if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
        errors.append(f"line {node.lineno}: use of forbidden name '{node.id}'")
    if isinstance(node, ast.Attribute):
        if node.attr in FORBIDDEN_NAMES or node.attr.startswith(FORBIDDEN_DUNDER_PREFIXES):
            errors.append(f"line {node.lineno}: access to forbidden attribute '{node.attr}'")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in FORBIDDEN_NAMES:
            errors.append(f"line {node.lineno}: call to forbidden function '{node.func.id}'")


def validate_manim_script(source: str) -> None:
    """Raise ScriptValidationError if `source` contains anything outside the
    allowlist. Returns None (no exception) if the script is clean."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ScriptValidationError(f"syntax error: {exc}", lineno=exc.lineno) from exc

    errors: list[str] = []
    has_scene_class = False

    for node in ast.walk(tree):
        errors.extend(_check_import(node))
        _check_name(node, errors)

        if isinstance(node, ast.ClassDef) and node.name == "MathVerseScene":
            has_scene_class = True

        # Reject string literals that look like real filesystem paths.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _looks_like_filesystem_path(node.value):
                errors.append(
                    f"line {node.lineno}: string literal looks like a filesystem "
                    f"path: {node.value!r}"
                )

    if not has_scene_class:
        errors.append("script must define `class MathVerseScene(Scene)`")

    if errors:
        raise ScriptValidationError("; ".join(errors))
