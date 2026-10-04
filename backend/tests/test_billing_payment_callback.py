"""GET /api/v1/billing/payment-callback: the page Paystack sends an invoice
payer's browser back to.

Its query string is attacker-controlled and it shares an origin with /admin
and /platform (tokens in localStorage), so: the reference is escaped, a strict
CSP forbids any script, and the page claims nothing about the outcome.

Pure unit tests: the route touches no database. No server, no DB, no Redis.
"""
from __future__ import annotations

import base64
import hashlib
import re

import httpx
import pytest

from src.app import app
from src.modules.billing import routes as billing_routes

PATH = "/api/v1/billing/payment-callback"
PAYLOAD = "<script>alert(1)</script><img src=x onerror=alert(2)>\"'"


async def get(params=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(PATH, params=params)


@pytest.mark.asyncio
@pytest.mark.parametrize("param", ["reference", "trxref"])
async def test_the_reference_is_escaped(param):
    res = await get({param: PAYLOAD})
    assert res.status_code == 200
    body = res.text
    assert "<script>alert" not in body and "<img" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&lt;img src=x onerror=alert(2)&gt;&quot;&#x27;" in body


@pytest.mark.asyncio
async def test_trxref_wins_over_reference():
    body = (await get({"trxref": "INV-trx", "reference": "INV-ref"})).text
    assert "INV-trx" in body and "INV-ref" not in body


@pytest.mark.asyncio
async def test_no_reference_means_no_reference_line():
    body = (await get()).text
    assert "Reference:" not in body


@pytest.mark.asyncio
async def test_the_page_claims_nothing_about_the_outcome():
    body = (await get({"reference": "INV-1"})).text.lower()
    for claim in ("payment received", "successful", "payment complete", "has been paid"):
        assert claim not in body, claim
    assert 'content="3;url=/admin/billing"' in body


@pytest.mark.asyncio
async def test_a_strict_csp_allows_only_the_pages_own_style():
    res = await get({"reference": "INV-1"})
    csp = res.headers["content-security-policy"]
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert directives["form-action"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert "script-src" not in directives, "no script source at all; default-src 'none' covers it"
    # The one style allowed is the page's own <style>, by hash.
    style = re.search(r"<style>(.*?)</style>", res.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert directives["style-src"] == f"'sha256-{digest}'"
    assert style == billing_routes._CALLBACK_STYLE
    assert "unsafe-inline" not in csp
