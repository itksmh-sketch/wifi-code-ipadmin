#!/usr/bin/env python3
"""Headless pre-check for the security headers and CSP.

Loads every page in scope in headless Chromium against a running instance of
the app and fails on anything the headers break: CSP violations (Report-Only
or enforced), framing refused by X-Frame-Options / frame-ancestors, COOP or
Permissions-Policy errors, and nosniff MIME refusals. It also runs the two
journeys most at risk: the admin Branding page's portal-preview iframe, and
the admin Billing "Pay Now" redirect to Paystack checkout.

Safe against any instance, production included, because it never touches the
API or real data: every /api/v1/* request the browser makes is intercepted
and answered from the fixtures below (pages get fake tokens, which never
leave the browser), and the Paystack checkout navigation is intercepted too,
so nothing reaches Paystack. The pages themselves (HTML, JS, CSS) and their
response headers come from the instance, which is the point. Portal pages'
own non-API calls (/portal/sites, /portal/branding/...) do reach it: read-only.

Standard library only (Chromium is driven over --remote-debugging-pipe), so it
runs on the host with nothing installed:

    python3 scripts/csp_precheck.py --base http://127.0.0.1:18000
    python3 scripts/csp_precheck.py --base https://ip-admin.duckdns.org --only /admin

Exit status 0 = clean, 1 = something the headers broke, 2 = could not run.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import glob
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ZERO = "00000000-0000-0000-0000-000000000000"
INVOICE_ID = "11111111-1111-1111-1111-111111111111"
PAYSTACK_STUB = "https://checkout.paystack.com/precheck"


def fake_jwt(payload: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}.precheck"


ADMIN_TOKEN = fake_jwt({"sub": ZERO, "role": "superadmin", "email": "precheck@example.test",
                        "isp_operator_id": ZERO, "exp": 9999999999})
SEED_TOKENS = f"""
try {{
  localStorage.setItem("access_token", {json.dumps(ADMIN_TOKEN)});
  localStorage.setItem("platform_access_token", "precheck");
  sessionStorage.setItem("reseller_access_token", "precheck");
}} catch (e) {{}}
"""

# (method, path) -> JSON body. Anything not listed: GET -> [], others -> {}.
FIXTURES = {
    ("GET", "/api/v1/billing/invoices"): [{
        "id": INVOICE_ID, "invoice_number": "INV-PRECHECK", "status": "issued", "amount_ghs": "150.00",
        "period_start": "2026-09-01", "period_end": "2026-09-30", "due_at": "2026-10-15", "paid_at": None,
    }],
    ("GET", "/api/v1/billing/status"): {"status": "approved", "billing_status": "active"},
    ("GET", "/api/v1/billing/onboarding-checklist"): {},
    ("POST", f"/api/v1/billing/invoices/{INVOICE_ID}/pay"): {"redirect_url": PAYSTACK_STUB},
    ("GET", "/api/v1/admin/branding"): {"portal_display_name": "Precheck"},
    ("GET", "/api/v1/admin/branding/preview-token"): {"token": "precheck", "preview_url": "/portal/login?rt=precheck"},
    ("GET", "/api/v1/platform/me"): {"name": "Precheck", "email": "precheck@example.test"},
    ("GET", "/api/v1/reseller/me"): {"email": "precheck@example.test"},
    ("GET", "/api/v1/reseller/wallet"): {"balance_ghs": "0", "lifetime_topped_up_ghs": "0", "lifetime_spent_ghs": "0"},
}

PAGES = [
    "/admin/login", "/admin/forgot-password", "/admin/", "/admin/analytics", "/admin/towns-sites", "/admin/routers",
    "/admin/plans", "/admin/vouchers", "/admin/sessions", "/admin/payment-credentials", "/admin/sms-credentials",
    "/admin/security", "/admin/branding", "/admin/billing",
    "/platform/login", "/platform/setup", "/platform/operators", "/platform/operators/new", f"/platform/operators/{ZERO}",
    "/platform/analytics", "/platform/billing", "/platform/transactions", "/platform/providers",
    "/platform/notification-templates", "/platform/health", "/platform/settings", "/platform/styleguide",
    "/reseller/login", "/reseller/dashboard", "/reseller/buy", "/reseller/vouchers", "/reseller/wallet",
    "/reseller/sales", "/reseller/print?codes=PRECHECK-1",
    "/apply",
    "/portal/login?gateway=10.5.50.1", f"/portal/pay?plan_id={ZERO}&site_id={ZERO}&gateway=10.5.50.1",
    "/portal/success?ref=precheck",
]

# Browser messages that mean a header got in the way.
SIGNALS = ("Content Security Policy", "Content-Security-Policy", "Refused to", "X-Frame-Options",
           "frame-ancestors", "Cross-Origin-Opener-Policy", "Permissions policy", "Permissions-Policy",
           "MIME type", "nosniff", "blocked by CORS")


class Chrome:
    """Minimal CDP client over --remote-debugging-pipe (fd 3 in, fd 4 out)."""

    def __init__(self, binary: str):
        self.profile = tempfile.mkdtemp(prefix="csp-precheck-")
        self._stderr = open(os.path.join(self.profile, "chrome-stderr.log"), "wb")
        to_chrome_r, self._to_chrome_w = os.pipe()
        self._from_chrome_r, from_chrome_w = os.pipe()
        # Park the child's ends above 4 first: os.pipe() may itself hand out
        # fd 3 or 4, and a direct dup2 onto 3/4 could clobber the other end.
        to_chrome_r_hi = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD, 10)
        from_chrome_w_hi = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD, 10)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        to_chrome_r, from_chrome_w = to_chrome_r_hi, from_chrome_w_hi

        def wire_fds():
            os.dup2(to_chrome_r, 3)
            os.dup2(from_chrome_w, 4)

        self.proc = subprocess.Popen(
            [binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-pipe",
             "--no-first-run", "--no-default-browser-check", f"--user-data-dir={self.profile}", "about:blank"],
            preexec_fn=wire_fds, close_fds=False, stdout=subprocess.DEVNULL, stderr=self._stderr,
        )
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._id = 0
        self._replies: dict[int, queue.Queue] = {}
        self.events: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        with os.fdopen(self._from_chrome_r, "rb", buffering=0) as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    return
                buf += chunk
                while b"\0" in buf:
                    raw, buf = buf.split(b"\0", 1)
                    msg = json.loads(raw)
                    if "id" in msg and msg["id"] in self._replies:
                        self._replies[msg["id"]].put(msg)
                    else:
                        self.events.put(msg)

    def send(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 15):
        with self._lock:
            self._id += 1
            mid = self._id
            self._replies[mid] = queue.Queue()
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self._to_chrome_w, json.dumps(msg).encode() + b"\0")
        reply = self._replies[mid].get(timeout=timeout)
        del self._replies[mid]
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    def close(self):
        try:
            self.send("Browser.close", timeout=5)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


class Page:
    def __init__(self, chrome: Chrome, base: str):
        self.c, self.base = chrome, base.rstrip("/")
        target = chrome.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.target = target
        self.s = chrome.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        for domain in ("Page", "Runtime", "Log", "Network"):
            chrome.send(f"{domain}.enable", session=self.s)
        chrome.send("Fetch.enable", {"patterns": [{"urlPattern": "*/api/v1/*"}, {"urlPattern": "https://checkout.paystack.com/*"}]},
                    session=self.s)
        chrome.send("Page.addScriptToEvaluateOnNewDocument", {"source": SEED_TOKENS}, session=self.s)
        self.messages: list[str] = []
        self.exceptions: list[str] = []
        self.navigations: list[str] = []
        self.doc_headers: dict[str, str] = {}
        self.paystack_hits = 0

    def _handle(self, ev: dict):
        if ev.get("sessionId") != self.s:
            return
        m, p = ev.get("method"), ev.get("params", {})
        if m == "Fetch.requestPaused":
            url, method = p["request"]["url"], p["request"]["method"]
            if url.startswith("https://checkout.paystack.com/"):
                self.paystack_hits += 1
                body = b"<!doctype html><title>paystack-stub</title><p>stub</p>"
                self.c.send("Fetch.fulfillRequest", {"requestId": p["requestId"], "responseCode": 200,
                            "responseHeaders": [{"name": "Content-Type", "value": "text/html"}],
                            "body": base64.b64encode(body).decode()}, session=self.s)
                return
            path = "/" + url.split("://", 1)[1].split("/", 1)[1].split("?", 1)[0]
            data = FIXTURES.get((method, path), [] if method == "GET" else {})
            self.c.send("Fetch.fulfillRequest", {"requestId": p["requestId"], "responseCode": 200,
                        "responseHeaders": [{"name": "Content-Type", "value": "application/json"}],
                        "body": base64.b64encode(json.dumps(data).encode()).decode()}, session=self.s)
        elif m == "Log.entryAdded":
            text = p["entry"].get("text", "")
            if any(sig in text for sig in SIGNALS):
                self.messages.append(f"[{p['entry'].get('level')}] {text}")
        elif m == "Runtime.consoleAPICalled":
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
            if any(sig in text for sig in SIGNALS):
                self.messages.append(f"[console] {text}")
        elif m == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            self.exceptions.append((d.get("exception") or {}).get("description", d.get("text", "")).split("\n")[0])
        elif m == "Page.frameNavigated":
            self.navigations.append(p["frame"].get("url", ""))
        elif m == "Network.responseReceived" and p.get("type") == "Document" and not self.doc_headers:
            self.doc_headers = {k.lower(): v for k, v in p["response"].get("headers", {}).items()}

    def pump(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self._handle(self.c.events.get(timeout=max(0.05, end - time.time())))
            except queue.Empty:
                pass

    def goto(self, path: str, settle: float = 4.0):
        self.c.send("Page.navigate", {"url": self.base + path}, session=self.s)
        self.pump(settle)

    def js(self, expression: str):
        # Evaluating from DevTools is not subject to the page's CSP.
        r = self.c.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True},
                        session=self.s)
        self.pump(0.2)
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.c.send("Target.closeTarget", {"targetId": self.target})
        except Exception:
            pass


def find_chrome() -> str | None:
    found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome")))
    return found[-1] if found else shutil.which("chromium") or shutil.which("google-chrome")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="e.g. http://127.0.0.1:18000")
    ap.add_argument("--chrome", default=find_chrome())
    ap.add_argument("--only", default="", help="only pages starting with this prefix")
    args = ap.parse_args()
    if not args.chrome or not os.path.exists(args.chrome):
        print("Chromium not found; pass --chrome", file=sys.stderr)
        return 2

    chrome = Chrome(args.chrome)
    failures, exceptions_seen = [], 0
    try:
        chrome.send("Browser.getVersion")
        for path in [p for p in PAGES if p.startswith(args.only)]:
            page = Page(chrome, args.base)
            try:
                page.goto(path)
                problems = list(page.messages)
                extra = ""
                if path == "/admin/branding":
                    state = page.js("""(() => { const f = document.querySelector('iframe[title="Portal preview"]');
                        if (!f) return 'no-iframe';
                        try { return 'ok ' + f.contentWindow.location.pathname + ' title=' + f.contentDocument.title; }
                        catch (e) { return 'blocked ' + e.name; } })()""")
                    page.pump(1.0)
                    problems += [m for m in page.messages if m not in problems]
                    extra = f"preview iframe: {state}"
                    if not str(state).startswith("ok /portal/login"):
                        problems.append(f"portal preview iframe did not load: {state}")
                if path == "/admin/billing":
                    clicked = page.js("""(() => { const b = [...document.querySelectorAll('button')]
                        .find(x => x.textContent.trim() === 'Pay Now'); if (!b) return false; b.click(); return true; })()""")
                    page.pump(4.0)
                    problems += [m for m in page.messages if m not in problems]
                    landed = any(u.startswith(PAYSTACK_STUB) for u in page.navigations)
                    extra = f"Pay Now clicked={clicked}, checkout navigation={landed}"
                    if not (clicked and landed and page.paystack_hits):
                        problems.append(f"Paystack redirect did not happen ({extra})")
                h = page.doc_headers
                summary = " ".join(f"{k}={'y' if k in h else '-'}" for k in (
                    "x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy",
                    "cross-origin-opener-policy", "content-security-policy", "content-security-policy-report-only"))
                status = "FAIL" if problems else "ok"
                print(f"{status:4} {path}\n     headers: {summary}" + (f"\n     {extra}" if extra else ""))
                for m in problems:
                    print(f"     ! {m[:300]}")
                if page.exceptions:
                    exceptions_seen += len(page.exceptions)
                    print(f"     (page JS exceptions, likely fixture shapes, not headers: {len(page.exceptions)}; "
                          f"first: {page.exceptions[0][:120]})")
                if problems:
                    failures.append(path)
            finally:
                page.close()
    except Exception as exc:
        print(f"could not run: {exc}", file=sys.stderr)
        return 2
    finally:
        chrome.close()
    print(f"\n{len(failures)} page(s) with problems" + (f": {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
