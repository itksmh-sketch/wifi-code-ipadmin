"""Phase 2 of reseller v2 (migrations 061/062), against a REAL database and a
REAL Redis:

  * the portal matches codes on vouchers.code_key: an old code works without
    its dashes, with spaces, in lower case; the column is the same expression
    as engine.code_key and unique platform-wide;
  * reseller allocations get 12-digit numeric codes with a Damm check digit,
    one digit longer past 10,000 live numeric codes per operator, chosen
    under a per-operator lock;
  * a numeric code with a wrong check digit is refused before the lookup
    (and before the limits) and isn't counted;
  * wrong codes ("not found" only) are limited per device, per no-MAC IP and
    per IP; found-but-refused codes and successes never count;
  * reaching the IP limit alerts the operator once an hour; the operator-wide
    threshold alerts the operator and the platform owner; each wrong code
    updates portal_login_failure_stats;
  * the limits are platform settings;
  * with Redis unreachable, logins fail open, and the platform owner is told.

Needs a disposable database and a disposable Redis (tests/throwaway_db.py run
--redis-url sets these), and the repo's freeradius/ directory:

    PORTAL_LOGIN_LIMITS_FLOW_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>
    PORTAL_LOGIN_LIMITS_FLOW_REDIS_URL=redis://<throwaway>
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import uuid

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("PORTAL_LOGIN_LIMITS_FLOW_DATABASE_URL", "")
FLOW_REDIS = os.getenv("PORTAL_LOGIN_LIMITS_FLOW_REDIS_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB
        or not FLOW_REDIS or os.getenv("REDIS_URL") != FLOW_REDIS,
        reason="needs PORTAL_LOGIN_LIMITS_FLOW_DATABASE_URL (a throwaway database) == DATABASE_URL "
               "and PORTAL_LOGIN_LIMITS_FLOW_REDIS_URL == REDIS_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB and FLOW_REDIS:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from redis.asyncio import Redis
    from sqlalchemy import cast, select, text
    from sqlalchemy.dialects.postgresql import INET
    from sqlalchemy.exc import IntegrityError

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AuditLog, PlatformOwner, PortalLoginFailureStat, Voucher
    from src.middleware import rate_limit
    from src.modules.notifications.email import service as email_service
    from src.modules.vouchers.engine import (
        code_key, damm_valid, generate_numeric_codes, generate_voucher_code, generate_voucher_password,
    )
    from src.portal import login_limits, routes as portal_routes
    from src.portal.routes import CHECK_DIGITS_DETAIL, EXPIRED_DETAIL, INVALID_CODE_DETAIL
    from src.utils.portal_token import create_portal_router_token
    from reseller_support import (
        make_admin, make_operator, make_plan, make_reseller, make_router, now, radius_accepts,
    )

DEVICE_429 = re.compile(r"^Too many wrong codes from this device\. Try again in \d+ minutes?\.$")
SITE_429 = re.compile(r"^Too many wrong codes on this hotspot\. Try again in \d+ minutes?\.$")


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def emails():
    """Every alert email, captured instead of sent."""
    sent = []

    class Capture:
        async def send(self, to, subject, body_html, body_text):
            sent.append({"to": to, "subject": subject, "text": body_text})

    mp = pytest.MonkeyPatch()
    mp.setattr(email_service, "get_email_service", lambda: Capture())
    yield sent
    mp.undo()


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def fresh_state(emails):
    login_limits.reset_limits_cache()
    login_limits.reset_fail_open_state()
    emails.clear()
    yield
    await login_limits.wait_for_alerts()


def random_ip() -> str:
    # 100.64.0.0/10 (shared address space): never a real site, unique per test.
    return f"100.{random.randint(64, 127)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


def random_mac() -> str:
    return ":".join(f"{random.randint(0, 255):02X}" for _ in range(6))


async def authenticate(router, code, *, ip, mac=None, **extra):
    transport = httpx.ASGITransport(app=app, client=(ip, 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        data = {"code": code, "rt": create_portal_router_token(str(router.id)), "gateway": "10.5.50.1", **extra}
        if mac is not None:
            data["mac"] = mac
        res = await c.post("/portal/authenticate", data=data)
    await login_limits.wait_for_alerts()
    return res


async def site():
    operator = await make_operator()
    router = await make_router(operator)
    plan = await make_plan(operator)
    return operator, router, plan


async def make_voucher(operator, plan, code=None, **fields) -> "Voucher":
    async with async_session_factory() as db:
        voucher = Voucher(isp_operator_id=operator.id, plan_id=plan.id, code=code or generate_voucher_code(16),
                          username=generate_voucher_code(16), password=generate_voucher_password(),
                          **{"status": "unused", **fields})
        db.add(voucher)
        await db.commit()
        await db.refresh(voucher)
        return voucher


async def unknown_numeric_code() -> str:
    """A well-formed 12-digit code (check digit right) that no voucher has."""
    while True:
        body = "".join(str(random.randint(0, 9)) for _ in range(11))
        from src.modules.vouchers.engine import damm_check_digit
        code = body + damm_check_digit(body)
        async with async_session_factory() as db:
            if (await db.execute(select(Voucher.id).where(Voucher.code_key == code))).first() is None:
                return code


def bad_check_digit(code: str) -> str:
    digits = code_key(code)
    wrong = str((int(digits[-1]) + 1) % 10)
    assert not damm_valid(digits[:-1] + wrong)
    return digits[:-1] + wrong


async def redis_values(*keys):
    return await rate_limit.get_rate_limit_redis().mget(*keys)


async def stats_row(operator, ip):
    async with async_session_factory() as db:
        return (await db.execute(select(PortalLoginFailureStat).where(
            PortalLoginFailureStat.isp_operator_id == operator.id,
            PortalLoginFailureStat.client_ip == cast(ip, INET),
        ))).scalar_one_or_none()


async def audit_for(action, operator_id=None):
    async with async_session_factory() as db:
        q = select(AuditLog).where(AuditLog.action == action)
        q = q.where(AuditLog.isp_operator_id == operator_id) if operator_id else q.where(AuditLog.isp_operator_id.is_(None))
        return (await db.execute(q)).scalars().all()


async def set_setting(key, value):
    async with async_session_factory() as db:
        await db.execute(text("INSERT INTO platform_settings (key, value) VALUES (:k, :v) "
                              "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"k": key, "v": value})
        await db.commit()
    login_limits.reset_limits_cache()


async def platform_owner() -> str:
    email = f"owner-{uuid.uuid4().hex[:8]}@throwaway.test"
    async with async_session_factory() as db:
        db.add(PlatformOwner(email=email, password_hash="x", name="Owner", is_active=True))
        await db.commit()
    return email


# ── code_key ──────────────────────────────────────────────────────────────

async def test_seeded_limits_are_5_30_50():
    async with async_session_factory() as db:
        rows = dict((await db.execute(text(
            "SELECT key, value FROM platform_settings WHERE key LIKE 'portal_login_fail_%'"))).all())
    assert rows == {"portal_login_fail_limit_mac": "5", "portal_login_fail_limit_ip": "30",
                    "portal_login_fail_alert_operator": "50"}


async def test_code_key_column_is_the_python_expression():
    inputs = ["ABCD-EFGH-JKLM-NPQR", " abcd efgh_jklm ", "1234-5678-9012", "ÄBC-déf", "１２３-45", "a\tb\nc", "x" * 40]
    async with async_session_factory() as db:
        for raw in inputs:
            sql = (await db.execute(text("SELECT upper(regexp_replace(:s, '[^A-Za-z0-9]', '', 'g'))"), {"s": raw})).scalar()
            assert sql == code_key(raw), raw
    operator, _, plan = await site()
    voucher = await make_voucher(operator, plan, code=f"ab-{uuid.uuid4().hex[:10]}")
    assert voucher.code_key == code_key(voucher.code)


async def test_code_key_is_unique_platform_wide():
    operator, _, plan = await site()
    other, _, other_plan = await site()
    voucher = await make_voucher(operator, plan)
    with pytest.raises(IntegrityError):
        await make_voucher(other, other_plan, code=voucher.code.lower().replace("-", " "))


async def test_old_code_works_without_dashes_with_spaces_and_in_lower_case():
    operator, router, plan = await site()
    voucher = await make_voucher(operator, plan)
    for typed in (voucher.code, voucher.code.replace("-", ""), voucher.code.lower(), voucher.code.replace("-", " "),
                  f"  {voucher.code.lower().replace('-', '')}  "):
        res = await authenticate(router, typed, ip=random_ip(), mac=random_mac())
        assert res.status_code == 200, (typed, res.text)
        assert (res.json()["username"], res.json()["password"]) == (voucher.username, voucher.password)
    assert await radius_accepts(voucher, router)


# ── numeric reseller codes ────────────────────────────────────────────────

async def test_allocated_codes_are_12_digit_numeric_and_log_in_however_typed():
    operator, router, plan = await site()
    _, headers = await make_admin(operator)
    reseller = await make_reseller(operator)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as c:
        res = await c.post(f"/admin/resellers/{reseller.id}/allocations", headers=headers,
                           json={"plan_id": str(plan.id), "quantity": 3})
    assert res.status_code == 200, res.text
    codes = res.json()["codes"]
    assert len(set(codes)) == 3
    for code in codes:
        assert re.fullmatch(r"\d{4}-\d{4}-\d{4}", code) and damm_valid(code_key(code)), code
        for typed in (code_key(code), code.replace("-", " ")):
            res = await authenticate(router, typed, ip=random_ip(), mac=random_mac())
            assert res.status_code == 200, (typed, res.text)
        async with async_session_factory() as db:
            v = (await db.execute(select(Voucher).where(Voucher.code == code))).scalar_one()
        assert not code_key(v.username).isdigit(), "usernames stay alphanumeric"
        assert await radius_accepts(v, router)


async def fill_numeric(operator, plan, count, status="unused"):
    """``count`` numeric vouchers for the operator, straight into the table."""
    prefix = f"9{random.randint(0, 99999):05d}"
    async with async_session_factory() as db:
        await db.execute(text("""
            INSERT INTO vouchers (isp_operator_id, plan_id, code, username, password, status, source)
            SELECT :op, :plan, :prefix || lpad(g::text, 6, '0'), 'fill-' || :prefix || '-' || g, 'x',
                   CAST(:status AS voucher_status), 'reseller'
            FROM generate_series(1, :n) AS g
        """), {"op": operator.id, "plan": plan.id, "prefix": prefix, "n": count, "status": status})
        await db.commit()


async def allocate_numeric(operator, plan, count) -> list[str]:
    async with async_session_factory() as db:
        codes = await generate_numeric_codes(db, operator.id, count)
        for code in codes:
            db.add(Voucher(isp_operator_id=operator.id, plan_id=plan.id, code=code, username=generate_voucher_code(16),
                           password="x", status="unused", source="reseller"))
        await db.commit()
    return codes


async def test_codes_grow_a_digit_past_10000_live_numeric_codes():
    operator, _, plan = await site()
    await fill_numeric(operator, plan, 9_998)
    await fill_numeric(operator, plan, 50, status="expired")  # not live: not counted
    assert {len(code_key(c)) for c in await allocate_numeric(operator, plan, 2)} == {12}  # 10,000 with the batch
    [code] = await allocate_numeric(operator, plan, 1)  # 10,001
    assert re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d", code) and damm_valid(code_key(code))
    other, _, other_plan = await site()  # per operator
    assert len(code_key((await allocate_numeric(other, other_plan, 1))[0])) == 12


async def test_two_allocations_cant_both_take_the_last_12_digit_slot():
    operator, _, plan = await site()
    await fill_numeric(operator, plan, 9_999)
    first = async_session_factory()
    db = await first.__aenter__()
    try:
        [code_a] = await generate_numeric_codes(db, operator.id, 1)  # takes the lock: 10,000 → 12 digits
        db.add(Voucher(isp_operator_id=operator.id, plan_id=plan.id, code=code_a, username=generate_voucher_code(16),
                       password="x", status="unused", source="reseller"))
        await db.flush()
        second = asyncio.create_task(allocate_numeric(operator, plan, 1))
        await asyncio.sleep(1.0)
        assert not second.done(), "the second allocation must wait for the first to commit"
        await db.commit()
    finally:
        await first.__aexit__(None, None, None)
    [code_b] = await asyncio.wait_for(second, 10)
    assert (len(code_key(code_a)), len(code_key(code_b))) == (12, 13)


# ── the check digit: refused first, never counted ─────────────────────────

async def test_wrong_check_digit_is_refused_before_the_lookup_and_not_counted(monkeypatch):
    operator, router, plan = await site()
    ip, mac = random_ip(), random_mac()

    async def must_not_run(*args, **kwargs):
        raise AssertionError("looked up a code whose check digit is wrong")

    with monkeypatch.context() as mp:
        mp.setattr(portal_routes, "_voucher_for_login", must_not_run)
        mp.setattr(portal_routes, "_resolve_operator", must_not_run)
        for _ in range(8):  # past every limit, were it counted
            code = bad_check_digit(await unknown_numeric_code())
            for typed in (code, f"{code[:4]}-{code[4:8]}-{code[8:]}"):
                res = await authenticate(router, typed, ip=ip, mac=mac)
                assert (res.status_code, res.json()["detail"]) == (400, CHECK_DIGITS_DETAIL)
    a = login_limits.Attempt(operator_id=str(operator.id), ip=ip, mac=login_limits.normalize_mac(mac), site_id=None)
    assert await redis_values(a.device_key, a.ip_key, a.operator_key) == [None, None, None]
    assert await stats_row(operator, ip) is None
    # ...while a well-formed unknown numeric code does count.
    res = await authenticate(router, await unknown_numeric_code(), ip=ip, mac=mac)
    assert (res.status_code, res.json()["detail"]) == (400, INVALID_CODE_DETAIL)
    assert await redis_values(a.device_key, a.ip_key, a.operator_key) == ["1", "1", "1"]


async def test_wrong_check_digit_is_reported_even_on_a_blocked_device():
    operator, router, plan = await site()
    ip, mac = random_ip(), random_mac()
    for _ in range(5):
        await authenticate(router, await unknown_numeric_code(), ip=ip, mac=mac)
    assert DEVICE_429.match((await authenticate(router, "NOPE-NOPE", ip=ip, mac=mac)).json()["detail"])
    res = await authenticate(router, bad_check_digit(await unknown_numeric_code()), ip=ip, mac=mac)
    assert (res.status_code, res.json()["detail"]) == (400, CHECK_DIGITS_DETAIL)


# ── the limits ────────────────────────────────────────────────────────────

async def test_device_limit_blocks_that_device_only():
    operator, router, plan = await site()
    voucher = await make_voucher(operator, plan)
    ip, mac = random_ip(), random_mac()
    for _ in range(5):
        res = await authenticate(router, f"NOPE-{uuid.uuid4().hex[:8]}", ip=ip, mac=mac.lower())
        assert (res.status_code, res.json()["detail"]) == (400, INVALID_CODE_DETAIL)
    for code in ("NOPE-NOPE-NOPE", voucher.code):  # blocked before the lookup: a good code too
        res = await authenticate(router, code, ip=ip, mac=mac.replace(":", "-"))
        assert res.status_code == 429 and DEVICE_429.match(res.json()["detail"]), res.text
    assert (await authenticate(router, voucher.code, ip=ip, mac=random_mac())).status_code == 200
    row = await stats_row(operator, ip)
    assert (row.failures, row.peak_device_window, row.peak_ip_window, row.device_limit_reached, row.ip_limit_reached) \
        == (5, 5, 5, 1, 0)
    assert row.site_id == router.site_id
    assert await audit_for("portal.login_failures_site_blocked", operator.id) == []


async def test_no_mac_requests_share_one_allowance_per_ip():
    operator, router, plan = await site()
    voucher = await make_voucher(operator, plan)
    ip = random_ip()
    for i in range(5):
        res = await authenticate(router, f"NOPE-{i}", ip=ip, mac=None if i % 2 else "not-a-mac")
        assert res.status_code == 400
    for mac in (None, "", "$(mac-esc)"):
        res = await authenticate(router, voucher.code, ip=ip, mac=mac)
        assert res.status_code == 429 and DEVICE_429.match(res.json()["detail"])
    assert (await authenticate(router, voucher.code, ip=ip, mac=random_mac())).status_code == 200


async def test_ip_limit_pauses_the_whole_site_and_alerts_the_operator_once_an_hour(emails):
    operator, router, plan = await site()
    voucher = await make_voucher(operator, plan)
    ip = random_ip()
    for i in range(30):
        res = await authenticate(router, f"NOPE-{i}", ip=ip, mac=random_mac())  # a new MAC every time
        assert res.status_code == 400, (i, res.text)
    res = await authenticate(router, voucher.code, ip=ip, mac=random_mac())
    assert res.status_code == 429 and SITE_429.match(res.json()["detail"])
    [row] = await audit_for("portal.login_failures_site_blocked", operator.id)
    assert (row.actor_type, row.detail["client_ip"], row.detail["failures_from_ip"]) == ("system", ip, 30)
    assert row.detail["site_name"] == "S"
    assert [e["to"] for e in emails] == [operator.contact_email]
    assert "code logins paused" in emails[0]["subject"] and ip in emails[0]["text"]
    stats = await stats_row(operator, ip)
    assert (stats.failures, stats.peak_ip_window, stats.ip_limit_reached) == (30, 30, 1)
    # Another site of the same operator, same hour: counted and blocked, but
    # no second "paused" alert. Its 30 take the operator to 60 in the window,
    # past 50, so the guessing alert goes out instead.
    ip2 = random_ip()
    for i in range(30):
        await authenticate(router, f"NOPE-{i}", ip=ip2, mac=random_mac())
    assert (await authenticate(router, voucher.code, ip=ip2, mac=random_mac())).status_code == 429
    assert len(await audit_for("portal.login_failures_site_blocked", operator.id)) == 1
    [guessing] = await audit_for("portal.login_failures_guessing", operator.id)
    assert (guessing.detail["failures_for_operator"], guessing.detail["connections"]) == (50, 2)
    assert ["code logins paused" in e["subject"] for e in emails] == [True, False]


async def test_guessing_alert_goes_to_the_operator_and_the_platform_owner(emails):
    owner = await platform_owner()
    operator, router, plan = await site()
    await set_setting("portal_login_fail_alert_operator", "8")
    try:
        ips = [random_ip() for _ in range(4)]
        for i in range(12):
            await authenticate(router, f"NOPE-{i}", ip=ips[i % 4], mac=random_mac())
    finally:
        await set_setting("portal_login_fail_alert_operator", "50")
    [row] = await audit_for("portal.login_failures_guessing", operator.id)
    assert (row.detail["failures_for_operator"], row.detail["connections"]) == (8, 4)
    recipients = sorted(e["to"] for e in emails)
    assert operator.contact_email in recipients and owner in recipients
    assert all("possible voucher code guessing" in e["subject"] for e in emails)
    peaks = [(await stats_row(operator, ip)).peak_operator_window for ip in ips]
    assert max(peaks) == 12


async def test_found_but_refused_codes_are_not_counted():
    operator, router, plan = await site()
    expired = await make_voucher(operator, plan, status="expired")
    ip, mac = random_ip(), random_mac()
    for _ in range(10):
        res = await authenticate(router, expired.code, ip=ip, mac=mac)
        assert (res.status_code, res.json()["detail"]) == (400, EXPIRED_DETAIL)
    a = login_limits.Attempt(operator_id=str(operator.id), ip=ip, mac=login_limits.normalize_mac(mac), site_id=None)
    assert await redis_values(a.device_key, a.ip_key, a.operator_key) == [None, None, None]
    assert await stats_row(operator, ip) is None


async def test_a_success_never_uses_up_the_allowance():
    operator, router, plan = await site()
    voucher = await make_voucher(operator, plan)
    ip, mac = random_ip(), random_mac()
    for _ in range(4):
        await authenticate(router, "NOPE-NOPE", ip=ip, mac=mac)
    for _ in range(5):
        assert (await authenticate(router, voucher.code, ip=ip, mac=mac)).status_code == 200
    a = login_limits.Attempt(operator_id=str(operator.id), ip=ip, mac=login_limits.normalize_mac(mac), site_id=None)
    assert await redis_values(a.device_key) == ["4"]
    assert (await authenticate(router, "NOPE-NOPE", ip=ip, mac=mac)).status_code == 400  # the 5th
    assert (await authenticate(router, voucher.code, ip=ip, mac=mac)).status_code == 429


async def test_limits_come_from_platform_settings():
    operator, router, plan = await site()
    ip, mac = random_ip(), random_mac()
    await set_setting("portal_login_fail_limit_mac", "2")
    try:
        assert [(await authenticate(router, "NOPE", ip=ip, mac=mac)).status_code for _ in range(3)] == [400, 400, 429]
    finally:
        await set_setting("portal_login_fail_limit_mac", "5")


# ── Redis unreachable: fail open, log, tell the platform owner ────────────

async def test_redis_down_fails_open_and_alerts_the_platform_owner(emails):
    owner = await platform_owner()
    operator, router, plan = await site()
    voucher = await make_voucher(operator, plan)
    ip, mac = random_ip(), random_mac()
    live = rate_limit.get_rate_limit_redis()
    rate_limit._redis_client = Redis.from_url("redis://127.0.0.1:1", decode_responses=True)
    before = len(await audit_for("portal.login_limits_unavailable"))
    try:
        for _ in range(8):  # past the device limit, were Redis there to count
            assert (await authenticate(router, "NOPE-NOPE", ip=ip, mac=mac)).status_code == 400
        assert (await authenticate(router, voucher.code, ip=ip, mac=mac)).status_code == 200
    finally:
        await rate_limit._redis_client.aclose()
        rate_limit._redis_client = live
    rows = await audit_for("portal.login_limits_unavailable")
    assert len(rows) == before + 1 and rows[-1].detail["error_type"] == "ConnectionError"
    assert owner in [e["to"] for e in emails]
    assert all("wrong-code limits are off" in e["subject"] for e in emails)
    stats = await stats_row(operator, ip)
    assert (stats.failures, stats.peak_device_window) == (8, 0)  # counted; the window peak unknown
