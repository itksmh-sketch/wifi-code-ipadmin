#!/usr/bin/env python3
"""Headless pre-check for the security headers and CSP.

Loads every page in scope in headless Chromium against a running instance of
the app and fails on anything the headers break: CSP violations (Report-Only
or enforced), framing refused by X-Frame-Options / frame-ancestors, COOP or
Permissions-Policy errors, and nosniff MIME refusals. It also runs the two
journeys most at risk: the admin Branding page's portal-preview iframe, and
the admin Billing "Pay Now" redirect to Paystack checkout. Then it clicks
through the buttons whose inline handlers phase B replaced (operators,
operator detail, reseller Logout and Print) and the one confirm() in scope
(notification templates, Reset to default), checking each still does what it
did: the right fixture call, dialog, or navigation.

Safe against any instance, production included, because it never touches the
API or real data: every /api/v1/* request the browser makes is intercepted
and answered from the fixtures below (pages get fake tokens, which never
leave the browser), any other request that is not a GET/HEAD is refused
before it leaves the browser, and the Paystack checkout navigation is
intercepted too, so nothing reaches Paystack. The pages themselves (HTML, JS, CSS) and their
response headers come from the instance, which is the point. The captive
portal's own JSON calls (sites, plans, initiate-payment, payment-status,
authenticate) are answered from PORTAL_FIXTURES too, so no payment or session
is ever created, and the router sign-in form post (http://10.5.50.1/login) is
captured in the browser; only /portal/branding/... reaches the instance
(read-only). The portal journey walks location -> plan -> pay -> voucher ->
Connect now, the same steps as the real-device test.

Origins: the hotspot reaches the portal over plain http on the server IP. To
check that origin off production, --map-host NAME maps a made-up host name to
127.0.0.1 so Chromium treats the page as insecure (127.0.0.1 itself counts as
a secure context); --forwarded-https sends X-Forwarded-Proto: https, which is
how the app sees the HTTPS site. --pass-reports lets the CSP report POST reach
the instance (staging only), to prove reports arrive.

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

# Click-through fixture ids.
OP_PENDING = "22222222-0000-0000-0000-000000000001"
OP_APPROVED = "22222222-0000-0000-0000-000000000002"
OP_SUSPENDED = "22222222-0000-0000-0000-000000000003"
APP_PENDING = "33333333-0000-0000-0000-000000000001"
APP_DECIDED = "33333333-0000-0000-0000-000000000002"
ADMIN_ID = "44444444-0000-0000-0000-000000000001"


def operator(op_id: str, name: str, status: str) -> dict:
    return {"id": op_id, "name": name, "slug": name.lower().replace(" ", "-"), "contact_email": "precheck@example.test",
            "status": status, "billing_status": "active", "admin_count": 1, "monthly_revenue_this_month": "0",
            "created_at": "2026-10-01T00:00:00Z"}


def application(app_id: str, name: str, status: str, operator_id: str | None = None) -> dict:
    return {"id": app_id, "isp_name": name, "contact_name": "Precheck Contact", "email": "precheck@example.test",
            "phone": "0244000000", "region": "Greater Accra", "expected_sites": 2, "message": "precheck",
            "status": status, "created_at": "2026-10-01T00:00:00Z", "reviewed_at": None if status == "pending"
            else "2026-10-02T00:00:00Z", "isp_operator_id": operator_id, "rejection_reason": None}


EMAIL_TEMPLATE = {
    "event": "precheck_event", "channel": "email", "label": "Precheck event", "description": "precheck",
    "subject": "Precheck subject", "body_text": "Hello precheck", "is_default": False, "placeholders": [],
    "body_html": '<p style="color:#123456">Hello <b>precheck</b></p>', "max_sms_segments": 3,
    "preview": {"channel": "email", "subject": "Precheck subject", "body_text": "Hello precheck",
                "body_html": '<p style="color:#123456">Hello <b>precheck</b></p>', "character_count": 14},
}

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
    # /platform/operators click-through
    ("GET", "/api/v1/platform/operators"): [operator(OP_PENDING, "Precheck Pending", "pending"),
                                            operator(OP_APPROVED, "Precheck Approved", "approved"),
                                            operator(OP_SUSPENDED, "Precheck Suspended", "suspended")],
    ("PUT", f"/api/v1/platform/operators/{OP_PENDING}/status"): operator(OP_PENDING, "Precheck Pending", "approved"),
    ("PUT", f"/api/v1/platform/operators/{OP_APPROVED}/status"): operator(OP_APPROVED, "Precheck Approved", "suspended"),
    ("PUT", f"/api/v1/platform/operators/{OP_SUSPENDED}/status"): operator(OP_SUSPENDED, "Precheck Suspended", "approved"),
    ("GET", "/api/v1/platform/applications"): [application(APP_PENDING, "Precheck Applicant", "pending"),
                                               application(APP_DECIDED, "Precheck Decided", "approved", OP_APPROVED)],
    ("PUT", f"/api/v1/platform/applications/{APP_PENDING}/approve"): {
        "temp_password": "precheck-temp", "slug": "precheck-applicant", "admin_email": "precheck@example.test",
        "temp_password_sms_sent": True},
    ("PUT", f"/api/v1/platform/applications/{APP_PENDING}/reject"): {},
    # /platform/operators/{id} click-through
    ("GET", f"/api/v1/platform/operators/{ZERO}"): operator(ZERO, "Precheck Detail", "approved"),
    ("GET", f"/api/v1/platform/operators/{ZERO}/infrastructure"): {
        "operator": operator(ZERO, "Precheck Detail", "approved"), "towns": [], "unassigned_routers": [],
        "unassigned_sites": [], "totals": {"towns": 0, "sites": 0, "routers": 0, "routers_online": 0, "plans": 0,
                                           "plans_active": 0}},
    ("GET", f"/api/v1/platform/operators/{ZERO}/admins"): [{
        "id": ADMIN_ID, "email": "precheck-admin@example.test", "role": "admin", "phone": "0244000000",
        "phone_verified": True, "is_active": True, "created_at": "2026-10-01T00:00:00Z"}],
    ("POST", f"/api/v1/platform/operators/{ZERO}/admins"): {
        "email": "precheck-new@example.test", "role": "admin", "temp_password": "precheck-temp",
        "temp_password_sms_sent": True, "phone": "0244000001"},
    ("POST", f"/api/v1/platform/operators/{ZERO}/admins/{ADMIN_ID}/reset-password"): {
        "email": "precheck-admin@example.test", "sms_sent": True, "phone": "0244000000", "mode": "reset"},
    # /platform/notification-templates click-through
    ("GET", "/api/v1/platform/notification-templates"): {"templates": [EMAIL_TEMPLATE]},
    ("POST", "/api/v1/platform/notification-templates/precheck_event/email/reset"): {**EMAIL_TEMPLATE, "is_default": True},
}

SITE_ID = "55555555-0000-0000-0000-000000000001"
PLAN_ID = "55555555-0000-0000-0000-000000000002"
PORTAL_REF = "precheck-ref"
VOUCHER = "PRECHECK-CODE"
GATEWAY = "http://10.5.50.1/"
PLAN = {"id": PLAN_ID, "name": "Precheck plan", "type": "time", "duration_minutes": 60, "data_limit_mb": None,
        "download_speed_kbps": 2000, "upload_speed_kbps": 1000, "price_ghs": "1.00", "site_id": SITE_ID}
# The captive portal's own JSON endpoints: (method, path) -> body. Never reach the instance.
PORTAL_FIXTURES = {
    ("GET", "/portal/sites"): [{"id": SITE_ID, "name": "Precheck Site", "town_name": "Precheck Town"}],
    ("GET", "/portal/plans"): [PLAN],
    ("POST", "/portal/initiate-payment"): {"internal_reference": PORTAL_REF, "status": "pending", "next_action": "wait",
                                           "display_message": "Approve the prompt on your phone."},
    ("POST", "/portal/continue-payment"): {"internal_reference": PORTAL_REF, "status": "pending", "next_action": "wait"},
    ("GET", "/portal/payment-status"): {"internal_reference": PORTAL_REF, "status": "success", "next_action": "none",
                                        "voucher_code": VOUCHER, "plan": PLAN, "display_message": "Payment successful."},
    ("POST", "/portal/authenticate"): {"success": True, "username": "precheck-user", "password": "precheck-pass"},
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

# Expected on a plain-http origin (the server IP): COOP needs a secure context,
# so the browser ignores it there. Listed as a note, not a failure.
INSECURE_ORIGIN_NOTES = ("The Cross-Origin-Opener-Policy header has been ignored, because the URL's origin was untrustworthy",)



def is_note(message: str) -> bool:
    return any(n in message for n in INSECURE_ORIGIN_NOTES)


# Browser messages that mean a header got in the way.
SIGNALS = ("Content Security Policy", "Content-Security-Policy", "Refused to", "X-Frame-Options",
           "frame-ancestors", "Cross-Origin-Opener-Policy", "Permissions policy", "Permissions-Policy",
           "MIME type", "nosniff", "blocked by CORS")


class Chrome:
    """Minimal CDP client over --remote-debugging-pipe (fd 3 in, fd 4 out)."""

    def __init__(self, binary: str, extra_args: list[str] | None = None):
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
             "--no-first-run", "--no-default-browser-check", f"--user-data-dir={self.profile}", *(extra_args or []),
             "about:blank"],
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

    def send(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 15,
             on_event=None):
        """on_event: handle events while waiting (a navigation or evaluate can
        depend on a paused request that only the event handler releases)."""
        with self._lock:
            self._id += 1
            mid = self._id
            self._replies[mid] = queue.Queue()
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self._to_chrome_w, json.dumps(msg).encode() + b"\0")
        end = time.time() + timeout
        while True:
            try:
                reply = self._replies[mid].get(timeout=0.05 if on_event else timeout)
                break
            except queue.Empty:
                if not on_event or time.time() > end:
                    raise TimeoutError(f"{method}: no reply in {timeout}s")
                while True:
                    try:
                        on_event(self.events.get_nowait())
                    except queue.Empty:
                        break
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
    def __init__(self, chrome: Chrome, base: str, forwarded_https: bool = False, pass_reports: bool = False):
        self.c, self.base = chrome, base.rstrip("/")
        self.pass_reports = pass_reports
        target = chrome.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.target = target
        self.s = chrome.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        for domain in ("Page", "Runtime", "Log", "Network"):
            chrome.send(f"{domain}.enable", session=self.s)
        # Every request is paused: /api/v1/* is answered from FIXTURES, a
        # non-GET/HEAD anywhere else is refused, the rest (pages, assets) goes on.
        chrome.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session=self.s)
        chrome.send("Page.addScriptToEvaluateOnNewDocument", {"source": SEED_TOKENS}, session=self.s)
        if forwarded_https:
            chrome.send("Network.setExtraHTTPHeaders", {"headers": {"X-Forwarded-Proto": "https"}}, session=self.s)
        # Out-of-process frames (the sandboxed email preview) get their own
        # session; attach to each so its console messages are seen too.
        self.children: dict[str, dict] = {}
        chrome.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                    session=self.s)
        self.messages: list[str] = []
        self.exceptions: list[str] = []
        self.navigations: list[str] = []
        self.doc_headers: dict[str, str] = {}
        self.paystack_hits = 0
        self.api_calls: list[tuple[str, str, str]] = []   # (method, path, body), all answered by FIXTURES
        self.refused: list[str] = []                       # non-API writes stopped in the browser
        self.dialogs: list[tuple[str, str]] = []           # (type, message) of alert/confirm/prompt
        self.dialog_accept = True
        self.portal_calls: list[tuple[str, str]] = []      # answered by PORTAL_FIXTURES
        self.gateway_posts: list[tuple[str, str, str]] = []  # router sign-in, captured in the browser
        self.reports_passed = 0

    def _handle(self, ev: dict):
        if ev.get("sessionId") != self.s and ev.get("sessionId") not in self.children:
            return
        m, p = ev.get("method"), ev.get("params", {})
        if m == "Target.attachedToTarget":
            child = p["sessionId"]
            self.children[child] = p["targetInfo"]
            for domain in ("Runtime", "Log"):
                self.c.send(f"{domain}.enable", session=child)
            self.c.send("Runtime.runIfWaitingForDebugger", session=child)
        elif m == "Fetch.requestPaused":
            url, method = p["request"]["url"], p["request"]["method"]
            if url.startswith("https://checkout.paystack.com/"):
                self.paystack_hits += 1
                body = b"<!doctype html><title>paystack-stub</title><p>stub</p>"
                self.c.send("Fetch.fulfillRequest", {"requestId": p["requestId"], "responseCode": 200,
                            "responseHeaders": [{"name": "Content-Type", "value": "text/html"}],
                            "body": base64.b64encode(body).decode()}, session=self.s)
                return
            if url.startswith(GATEWAY):
                self.gateway_posts.append((method, url, p["request"].get("postData", "")))
                body = b"<!doctype html><title>router-stub</title><p>signed in (stub)</p>"
                self.c.send("Fetch.fulfillRequest", {"requestId": p["requestId"], "responseCode": 200,
                            "responseHeaders": [{"name": "Content-Type", "value": "text/html"}],
                            "body": base64.b64encode(body).decode()}, session=self.s)
                return
            path = "/" + url.split("://", 1)[1].split("/", 1)[1].split("?", 1)[0]
            if (method, path) in PORTAL_FIXTURES:
                self.portal_calls.append((method, path))
                self.c.send("Fetch.fulfillRequest", {"requestId": p["requestId"], "responseCode": 200,
                            "responseHeaders": [{"name": "Content-Type", "value": "application/json"}],
                            "body": base64.b64encode(json.dumps(PORTAL_FIXTURES[(method, path)]).encode()).decode()},
                            session=self.s)
                return
            if path == "/api/v1/csp-report" and self.pass_reports:
                self.reports_passed += 1
                self.api_calls.append((method, path, p["request"].get("postData", "")))
                self.c.send("Fetch.continueRequest", {"requestId": p["requestId"]}, session=self.s)
                return
            if not path.startswith("/api/v1/"):
                if method in ("GET", "HEAD"):
                    self.c.send("Fetch.continueRequest", {"requestId": p["requestId"]}, session=self.s)
                else:
                    self.refused.append(f"{method} {url}")
                    self.c.send("Fetch.failRequest", {"requestId": p["requestId"], "errorReason": "BlockedByClient"},
                                session=self.s)
                return
            self.api_calls.append((method, path, p["request"].get("postData", "")))
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
        elif m == "Page.javascriptDialogOpening":
            self.dialogs.append((p.get("type", ""), p.get("message", "")))
            self.c.send("Page.handleJavaScriptDialog", {"accept": self.dialog_accept}, session=self.s)
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
        self.c.send("Page.navigate", {"url": self.base + path}, session=self.s, on_event=self._handle)
        self.pump(settle)

    def js(self, expression: str):
        # Evaluating from DevTools is not subject to the page's CSP.
        r = self.c.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True},
                        session=self.s, on_event=self._handle)
        self.pump(0.2)
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.c.send("Target.closeTarget", {"targetId": self.target})
        except Exception:
            pass


# ── Click-through scenarios ─────────────────────────────────────────────────
# Each clicks real buttons by their visible text (so the same scenario runs
# against the old inline-handler pages and the new data-action ones) and checks
# what followed: the fixture calls made, dialogs shown, overlay state, URL.
# Clicks go through setTimeout so a confirm()/alert() can't block the
# evaluate call; the dialog is answered from the event pump.

FIND = """((scope, rowText, label) => {
  const roots = scope ? [...document.querySelectorAll(scope)] : [document];
  for (const root of roots) {
    const rows = rowText ? [...root.querySelectorAll('tr, .modal-dialog, div')].filter(r => r.textContent.includes(rowText))
                         : [root];
    for (const r of rows.reverse()) {
      const el = [...r.querySelectorAll('button, a')].find(b => b.textContent.trim() === label && b.offsetParent !== null);
      if (el) return el;
    }
  }
  return null;
})"""


class Steps:
    def __init__(self, page: Page):
        self.page, self.results = page, []

    def click(self, label: str, scope: str = "", row: str = "", accept: bool = True, settle: float = 1.0):
        self.page.dialog_accept = accept
        mark = (len(self.page.api_calls), len(self.page.dialogs))
        found = self.page.js(f"""(() => {{ const el = {FIND}({json.dumps(scope)}, {json.dumps(row)}, {json.dumps(label)});
            if (!el) return false; setTimeout(() => el.click(), 0); return true; }})()""")
        self.page.pump(settle)
        calls = [c for c in self.page.api_calls[mark[0]:] if c[1] != "/api/v1/csp-report"]
        return found, calls, self.page.dialogs[mark[1]:]

    def visible(self, element_id: str) -> bool:
        return bool(self.page.js(f"""(() => {{ const el = document.getElementById({json.dumps(element_id)});
            return !!el && el.offsetParent !== null; }})()"""))

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append((name, bool(ok), detail))


def writes(calls):
    return [(m, p) for m, p, _ in calls if m != "GET"]


def scenario_operators(page: Page) -> Steps:
    st = Steps(page)
    for label, row, op_id, status in (("Suspend", "Precheck Approved", OP_APPROVED, "suspended"),
                                      ("Unsuspend", "Precheck Suspended", OP_SUSPENDED, "approved"),
                                      ("Approve", "Precheck Pending", OP_PENDING, "approved")):
        found, calls, dialogs = st.click(label, "#tbody", row)
        want = ("PUT", f"/api/v1/platform/operators/{op_id}/status")
        body = next((json.loads(b or "{}") for m, p, b in calls if (m, p) == want), None)
        st.check(f"operator row {label}: PUT status={status}, dialogs={dialogs or 'none'}",
                 found and writes(calls) == [want] and body == {"status": status}, f"calls={writes(calls)} body={body}")

    url_before = page.js("location.href")
    found, calls, _ = st.click("Precheck Applicant", "#apps-tbody")
    st.check("application name link opens the detail dialog, URL unchanged (no '#')",
             found and st.visible("modal-detail") and page.js("location.href") == url_before and not writes(calls))
    found, calls, _ = st.click("Close", "#detail-actions")
    st.check("detail dialog Close (pending) closes it", found and not st.visible("modal-overlay") and not writes(calls))

    found, calls, _ = st.click("View", "#apps-tbody", "Precheck Applicant")
    st.check("application row View opens the detail dialog", found and st.visible("modal-detail") and not writes(calls))
    found, calls, _ = st.click("Reject", "#detail-actions")
    st.check("detail dialog Reject opens the reject dialog, no API write", found and st.visible("modal-reject") and not writes(calls))
    found, calls, _ = st.click("Cancel", "#modal-reject")
    st.check("reject dialog Cancel closes it", found and not st.visible("modal-overlay") and not writes(calls))

    st.click("View", "#apps-tbody", "Precheck Applicant")
    found, calls, _ = st.click("Approve", "#detail-actions")
    st.check("detail dialog Approve opens the approve dialog, no API write", found and st.visible("modal-approve") and not writes(calls))
    found, calls, _ = st.click("Cancel", "#modal-approve")
    st.check("approve dialog Cancel closes it", found and not st.visible("modal-overlay") and not writes(calls))

    found, calls, _ = st.click("Approve", "#apps-tbody", "Precheck Applicant")
    st.check("application row Approve opens the approve dialog and does not approve yet",
             found and st.visible("modal-approve") and not writes(calls), f"calls={writes(calls)}")
    found, calls, _ = st.click("Approve operator", "#modal-approve")
    st.check("approve dialog submit: PUT approve, then the one-time password dialog",
             found and writes(calls) == [("PUT", f"/api/v1/platform/applications/{APP_PENDING}/approve")]
             and st.visible("modal-password"), f"calls={writes(calls)}")
    found, calls, _ = st.click("Done", "#modal-password")
    st.check("password dialog Done closes it", found and not st.visible("modal-overlay") and not writes(calls))

    found, calls, _ = st.click("Reject", "#apps-tbody", "Precheck Applicant")
    st.check("application row Reject opens the reject dialog and does not reject yet",
             found and st.visible("modal-reject") and not writes(calls))
    found, calls, dialogs = st.click("Reject application", "#modal-reject")
    st.check("reject submit with no reason: alert shown, no API write",
             found and dialogs == [("alert", "Enter a rejection reason.")] and not writes(calls), f"dialogs={dialogs}")
    page.js("document.getElementById('reject-reason').value = 'precheck reason'")
    found, calls, dialogs = st.click("Reject application", "#modal-reject")
    st.check("reject submit with a reason: PUT reject, dialog closes",
             found and writes(calls) == [("PUT", f"/api/v1/platform/applications/{APP_PENDING}/reject")]
             and not st.visible("modal-overlay"), f"calls={writes(calls)}")

    found, calls, _ = st.click("View", "#apps-tbody", "Precheck Decided")
    st.check("decided application View opens the detail dialog", found and st.visible("modal-detail"))
    found, calls, _ = st.click("Close", "#detail-actions")
    st.check("detail dialog Close (decided) closes it", found and not st.visible("modal-overlay") and not writes(calls))
    return st


def scenario_operator_detail(page: Page) -> Steps:
    st = Steps(page)
    found, calls, _ = st.click("Edit")
    st.check("Edit opens the operator dialog", found and st.visible("modal-operator"))
    found, calls, _ = st.click("Cancel", "#modal-operator")
    st.check("operator dialog Cancel closes it", found and not st.visible("modal-overlay") and not writes(calls))

    page.js("document.getElementById('add-admin-btn').click()")
    page.pump(0.5)
    found, calls, _ = st.click("Cancel", "#modal-admin")
    st.check("add-admin dialog Cancel closes it", found and not st.visible("modal-overlay") and not writes(calls))
    page.js("""document.getElementById('add-admin-btn').click();
        document.getElementById('admin-email').value = 'precheck-new@example.test';
        document.getElementById('admin-phone').value = '0244000001';""")
    page.pump(0.5)
    found, calls, _ = st.click("Create admin", "#modal-admin")
    st.check("Create admin: POST admins, then the result dialog",
             found and writes(calls) == [("POST", f"/api/v1/platform/operators/{ZERO}/admins")]
             and st.visible("modal-admin-done"), f"calls={writes(calls)}")
    found, calls, _ = st.click("Done", "#modal-admin-done")
    st.check("admin-created dialog Done closes it", found and not st.visible("modal-overlay") and not writes(calls))

    found, calls, _ = st.click("Reset password", "#admins-body")
    st.check("row Reset password opens the confirmation dialog, no API write",
             found and st.visible("modal-reset") and not writes(calls))
    found, calls, _ = st.click("Cancel", "#modal-reset")
    st.check("reset dialog Cancel closes it", found and not st.visible("modal-overlay") and not writes(calls))
    st.click("Reset password", "#admins-body")
    found, calls, _ = st.click("Reset password", "#modal-reset")
    st.check("reset dialog submit: POST reset-password, then the result dialog",
             found and writes(calls) == [("POST", f"/api/v1/platform/operators/{ZERO}/admins/{ADMIN_ID}/reset-password")]
             and st.visible("modal-reset-done"), f"calls={writes(calls)}")
    found, calls, _ = st.click("Done", "#modal-reset-done")
    st.check("reset result dialog Done closes it", found and not st.visible("modal-overlay") and not writes(calls))
    return st


def scenario_notification_templates(page: Page) -> Steps:
    st = Steps(page)
    # The sandboxed srcdoc frame runs out of process: evaluate in its own session.
    child = next((sid for sid, info in page.children.items() if info.get("url") == "about:srcdoc"), None)
    shown = None
    if child:
        r = page.c.send("Runtime.evaluate", {"expression": "document.body.innerText + ' | ' + "
                        "getComputedStyle(document.querySelector('p')).color", "returnByValue": True}, session=child)
        shown = r.get("result", {}).get("value")
    st.check("email preview iframe renders the fixture HTML, style attribute applied",
             shown == "Hello precheck | rgb(18, 52, 86)", f"frame={bool(child)} shown={shown!r}")
    found, calls, dialogs = st.click("Reset to default", accept=False)
    st.check("Reset to default: confirm() fires; dismissing it makes no API write",
             found and [d[0] for d in dialogs] == ["confirm"] and not writes(calls), f"dialogs={dialogs} calls={writes(calls)}")
    found, calls, dialogs = st.click("Reset to default", accept=True)
    st.check("Reset to default: accepting the confirm() makes the POST reset",
             found and [d[0] for d in dialogs] == ["confirm"]
             and writes(calls) == [("POST", "/api/v1/platform/notification-templates/precheck_event/email/reset")],
             f"dialogs={dialogs} calls={writes(calls)}")
    return st


def scenario_reseller_logout(page: Page) -> Steps:
    st = Steps(page)
    nav_mark = len(page.navigations)
    token_after = page.js(f"""(() => {{ const el = {FIND}('', '', 'Logout'); if (!el) return 'no-button';
        el.click(); return sessionStorage.getItem('reseller_access_token'); }})()""")
    page.pump(2.0)
    landed = any(u.split("?")[0].endswith("/reseller/login") for u in page.navigations[nav_mark:])
    st.check("Logout clears the token and lands on /reseller/login", token_after is None and landed,
             f"token={token_after!r} landed={landed}")
    return st


def scenario_reseller_print(page: Page) -> Steps:
    st = Steps(page)
    page.js("window.__printed = 0; window.print = () => { window.__printed += 1; }")
    found, calls, _ = st.click("Print")
    printed = page.js("window.__printed")
    st.check("Print calls window.print", found and printed == 1, f"printed={printed}")
    return st


def scenario_portal_journey(page: Page) -> Steps:
    """Location -> plan -> pay -> voucher -> Connect now, on fixtures."""
    st = Steps(page)
    page.pump(1.0)
    picked = page.js(f"""(() => {{ const s = document.getElementById('siteSelect');
        if (!s || !s.options.length) return false; s.value = {json.dumps(SITE_ID)};
        s.dispatchEvent(new Event('change')); return true; }})()""")
    page.pump(1.5)
    st.check("location picker lists the fixture site and selecting it loads plans",
             picked and bool(page.js("!!document.querySelector('button[data-plan]')")))
    found, _, _ = st.click(f"Buy - GHS 1.00", "#plansGrid", settle=3.0)
    on_pay = page.js("location.pathname") == "/portal/pay"
    st.check("Buy opens the pay page", found and on_pay, f"at={page.js('location.pathname')}")
    page.pump(1.0)
    page.js("document.getElementById('phone').value = '0244000000'")
    submitted = page.js("""(() => { const f = document.getElementById('payForm'); if (!f) return false;
        setTimeout(() => f.requestSubmit ? f.requestSubmit() : f.submit(), 0); return true; })()""")
    for _ in range(40):  # the pay page polls every 5 s
        page.pump(0.5)
        if page.js("location.pathname") == "/portal/success":
            break
    st.check("Pay: initiate-payment, then payment-status, then the success page",
             submitted and ("POST", "/portal/initiate-payment") in page.portal_calls
             and page.js("location.pathname") == "/portal/success", f"calls={page.portal_calls}")
    page.pump(2.0)
    code = page.js("(document.getElementById('voucherCode') || {}).textContent")
    hint = page.js("""(() => { const p = [...document.querySelectorAll('#mainBox p.muted.mt-8')];
        return p.length && p[0].offsetParent !== null ? p[0].textContent : null; })()""")
    margin = page.js("""(() => { const p = document.querySelector('#mainBox p.muted.mt-8');
        return p ? getComputedStyle(p).marginTop : null; })()""")
    st.check("success page shows the voucher and the save-it-now line (class styled)",
             code == VOUCHER and hint and hint.startswith("Save this code now") and margin == "8px",
             f"code={code!r} hint={hint!r} margin={margin!r}")
    found, _, _ = st.click("Connect now", settle=3.0)
    post = next((g for g in page.gateway_posts if g[0] == "POST"), None)
    authed = found and ("POST", "/portal/authenticate") in page.portal_calls
    if page.base.startswith("https://") and post is None and page.navigations[-1:] == ["chrome-error://chromewebdata/"]:
        # Chromium refuses a form post from an https page to http://<gateway>
        # (mixed-form block, with or without CSP; confirmed 2026-10-05 with
        # Page.setBypassCSP). Hotspot clients use the plain-http IP origin.
        st.check("Connect now: authenticate; the https->http gateway post is the browser's mixed-form block "
                 "(expected on the HTTPS address, not CSP)", authed)
        return st
    st.check("Connect now: authenticate, then the sign-in form posts to the router gateway",
             authed and post is not None and "username=precheck-user" in post[2],
             f"gateway_posts={page.gateway_posts} nav={page.navigations[-2:]}")
    return st


SCENARIOS = {
    "/portal/login?gateway=10.5.50.1": scenario_portal_journey,
    "/platform/operators": scenario_operators,
    f"/platform/operators/{ZERO}": scenario_operator_detail,
    "/platform/notification-templates": scenario_notification_templates,
    "/reseller/dashboard": scenario_reseller_logout,
    "/reseller/buy": scenario_reseller_logout,
    "/reseller/vouchers": scenario_reseller_logout,
    "/reseller/wallet": scenario_reseller_logout,
    "/reseller/sales": scenario_reseller_logout,
    "/reseller/print?codes=PRECHECK-1": scenario_reseller_print,
}


def find_chrome() -> str | None:
    found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome")))
    return found[-1] if found else shutil.which("chromium") or shutil.which("google-chrome")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="e.g. http://127.0.0.1:18000")
    ap.add_argument("--chrome", default=find_chrome())
    ap.add_argument("--only", default="", help="only pages starting with this prefix")
    ap.add_argument("--no-clicks", action="store_true", help="page loads only, skip the click-through scenarios")
    ap.add_argument("--map-host", default="", help="made-up host name to resolve to 127.0.0.1 (insecure origin)")
    ap.add_argument("--forwarded-https", action="store_true", help="send X-Forwarded-Proto: https (staging)")
    ap.add_argument("--pass-reports", action="store_true", help="let CSP report POSTs reach the instance (staging)")
    args = ap.parse_args()
    if not args.chrome or not os.path.exists(args.chrome):
        print("Chromium not found; pass --chrome", file=sys.stderr)
        return 2

    extra = [f"--host-resolver-rules=MAP {args.map_host} 127.0.0.1"] if args.map_host else []
    chrome = Chrome(args.chrome, extra)
    failures, exceptions_seen = [], 0
    clicks_total, api_writes, refused = 0, [], []
    try:
        chrome.send("Browser.getVersion")
        for path in [p for p in PAGES if p.startswith(args.only)]:
            page = Page(chrome, args.base, args.forwarded_https, args.pass_reports)
            try:
                page.goto(path)
                problems = [m for m in page.messages if not is_note(m)]
                extra = ""
                if path == "/admin/branding":
                    state = page.js("""(() => { const f = document.querySelector('iframe[title="Portal preview"]');
                        if (!f) return 'no-iframe';
                        try { return 'ok ' + f.contentWindow.location.pathname + ' title=' + f.contentDocument.title; }
                        catch (e) { return 'blocked ' + e.name; } })()""")
                    page.pump(1.0)
                    problems += [m for m in page.messages if m not in problems and not is_note(m)]
                    extra = f"preview iframe: {state}"
                    if not str(state).startswith("ok /portal/login"):
                        problems.append(f"portal preview iframe did not load: {state}")
                if path == "/admin/billing":
                    clicked = page.js("""(() => { const b = [...document.querySelectorAll('button')]
                        .find(x => x.textContent.trim() === 'Pay Now'); if (!b) return false; b.click(); return true; })()""")
                    page.pump(4.0)
                    problems += [m for m in page.messages if m not in problems and not is_note(m)]
                    landed = any(u.startswith(PAYSTACK_STUB) for u in page.navigations)
                    extra = f"Pay Now clicked={clicked}, checkout navigation={landed}"
                    if not (clicked and landed and page.paystack_hits):
                        problems.append(f"Paystack redirect did not happen ({extra})")
                if path in SCENARIOS and not args.no_clicks:
                    steps = SCENARIOS[path](page)
                    problems += [m for m in page.messages if m not in problems and not is_note(m)]
                    for name, ok, detail in steps.results:
                        clicks_total += 1
                        extra += ("\n     " if extra else "") + f"{'click ok' if ok else 'CLICK FAIL'}: {name}" + \
                                 ("" if ok else f" ({detail})")
                        if not ok:
                            problems.append(f"click-through failed: {name} ({detail})")
                api_writes += [f"{m} {p}" for m, p, _ in page.api_calls if m != "GET" and p != "/api/v1/csp-report"]
                api_writes += [f"{m} {p}" for m, p in page.portal_calls if m != "GET"]
                api_writes += [f"{m} {u} (router gateway, captured)" for m, u, _ in page.gateway_posts if m != "GET"]
                reports = [c for c in page.api_calls if c[1] == "/api/v1/csp-report"]
                if reports:
                    problems.append(f"{len(reports)} CSP report(s) sent: {[r[2][:200] for r in reports]}")
                if page.refused:
                    refused += page.refused
                    problems.append(f"non-API write refused in the browser: {page.refused}")
                h = page.doc_headers
                summary = " ".join(f"{k}={'y' if k in h else '-'}" for k in (
                    "x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy",
                    "cross-origin-opener-policy", "content-security-policy", "content-security-policy-report-only",
                    "reporting-endpoints"))
                csp = h.get("content-security-policy-report-only", "")
                if csp:
                    summary += f" report-to={'y' if 'report-to' in csp else '-'}"
                status = "FAIL" if problems else "ok"
                print(f"{status:4} {path}\n     headers: {summary}" + (f"\n     {extra}" if extra else ""))
                for n in sorted({m for m in page.messages if is_note(m)}):
                    print(f"     note (expected on an insecure origin): {n[:110]}")
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
    print(f"\nclick-through checks: {clicks_total}; API writes made, every one answered by a fixture: "
          f"{len(api_writes)}; non-API writes refused in the browser: {len(refused)}")
    for w in api_writes:
        print(f"     fixture: {w}")
    print(f"{len(failures)} page(s) with problems" + (f": {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
