"""Source guard: operator-facing notification links are built by
dispatcher.app_url, which reads the platform_app_url setting from the database.

A new notification or job that reaches for ``settings.platform_app_url``
directly would send the .env address (on production the bare server IP over
plain http) instead of the public HTTPS domain. The one permitted read of the
config value is app_url's own fallback.

Pure unit test: no server, no DB.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCANNED = [BACKEND / "src/modules/notifications/dispatcher.py", *sorted((BACKEND / "src/jobs").glob("*.py"))]


def _config_reads(path: Path) -> list[tuple[str, int]]:
    """(enclosing function, line) of every ``<x>.platform_app_url`` attribute read."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    found = []
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                if isinstance(node, ast.Attribute) and node.attr == "platform_app_url":
                    found.append((func.name, node.lineno))
    return found


def test_only_app_url_reads_the_config_value():
    offenders = []
    for path in SCANNED:
        for func, line in _config_reads(path):
            if not (path.name == "dispatcher.py" and func == "app_url"):
                offenders.append(f"{path.relative_to(BACKEND)}:{line} in {func}()")
    assert offenders == [], "build links with dispatcher.app_url(path), not settings.platform_app_url: " + ", ".join(offenders)


def test_app_url_keeps_its_fallback():
    """Negative: the helper must still fall back to config, or a database
    hiccup would send a link with no host."""
    reads = _config_reads(BACKEND / "src/modules/notifications/dispatcher.py")
    assert [f for f, _ in reads] == ["app_url"]
