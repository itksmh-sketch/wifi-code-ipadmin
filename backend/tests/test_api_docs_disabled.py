"""The API docs and schema are not served. No server or database needed.

/docs, /redoc and /openapi.json were public and listed every route; the
access logs showed only scanners fetching them. This pins them off so a
future change to the FastAPI(...) call can't quietly bring them back.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.app import app


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_and_schema_are_not_served(path):
    res = TestClient(app).get(path)
    assert res.status_code == 404, f"{path} -> {res.status_code}"


def test_docs_are_disabled_in_the_app_config():
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None


def test_the_schema_can_still_be_generated_locally():
    """What the README tells people to use instead."""
    schema = app.openapi()
    assert schema["paths"], "app.openapi() should still build the schema"
