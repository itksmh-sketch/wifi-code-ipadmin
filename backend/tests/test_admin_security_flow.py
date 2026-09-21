"""PIN, lockouts, phone re-verification and the gated areas, against a REAL database.

Runs the FastAPI app in-process (no server) with every SMS send captured
instead of sent, following tests/test_admin_accounts_flow.py. It writes
operators, admins, codes and security events, so it only runs when explicitly
pointed at a disposable database:

    ADMIN_FLOW_TEST_DATABASE_URL=postgresql+asyncpg://.../<name containing "throwaway">
    DATABASE_URL=<the same URL>

and skips otherwise — including in the production container, whose
DATABASE_URL is the live database.

Time-dependent behaviour (a lapsed 3h lock, an expired 15-minute elevation, a
phone change ageing out of a 30-day window) is exercised by writing the stored
timestamp into the past rather than by waiting or by freezing the clock. That
is deliberate: these are all "is this stored instant still in the future"
comparisons against now(), so moving the instant tests exactly the branch that
production will take, and the test stays honest if the implementation changes
how it derives the deadline.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

FLOW_DB = os.getenv("ADMIN_FLOW_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not FLOW_DB or "throwaway" not in FLOW_DB.rsplit("/", 1)[-1] or os.getenv("DATABASE_URL") != FLOW_DB,
        reason="needs ADMIN_FLOW_TEST_DATABASE_URL (a throwaway database) == DATABASE_URL",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

if FLOW_DB:  # imports that bind the engine only happen when the guard can pass
    import httpx
    from sqlalchemy import select

    from src.app import app
    from src.db.base import async_session_factory
    from src.db.models import AdminSecurityEvent, AdminUser, ISPOperator, PlatformOwner
    from src.middleware import auth as auth_middleware
    from src.modules.admin_accounts import lockout
    from src.modules.admin_accounts import notifications as account_notifications
    from src.modules.admin_accounts import platform_reset
    from src.modules.admin_accounts import routes as account_routes
    from src.modules.admin_accounts.lockout import LOCKOUT_HOURS, LOGIN_MAX_ATTEMPTS, PIN_MAX_ATTEMPTS
    from src.modules.admin_accounts.pins import PIN_ELEVATION_MINUTES
    from src.modules.admin_accounts.routes import PHONE_CHANGE_LIMIT, PHONE_CHANGE_WINDOW_DAYS
    from src.modules.auth import routes as auth_routes
    from src.modules.sms.types import SMSSendResult
    from src.utils.auth import hash_password

PASSWORD = "Str0ngPassword"
NEW_PASSWORD = "Ev3nStrongerPass"
PIN = "4729"
OTHER_PIN = "8315"
PHONE = "233244000111"

# Every gated write, as (method, path template). Kept in one place so a route
# that loses its gate fails the state matrix rather than silently going open.
GATED = [
    ("PUT", "/payment-credentials/paystack"),
    ("POST", "/payment-credentials/paystack/activate"),
    ("DELETE", "/payment-credentials/paystack"),
    ("PUT", "/sms-credentials/arkesel"),
    ("POST", "/sms-credentials/activate-platform"),
    ("POST", "/auth/me/phone"),
    ("POST", "/auth/me/security-question"),
]


class Outbox:
    """Captures every SMS the code under test tries to send."""

    def __init__(self):
        self.sent = []  # (kind, phone, payload)

    async def otp(self, phone, code, *, purpose):
        self.sent.append((purpose, phone, code))
        return SMSSendResult(success=True, provider_reference="test")

    async def lockout_sms(self, admin, *, kind):
        self.sent.append((f"lockout_{kind}", admin.phone, None))
        return SMSSendResult(success=True, provider_reference="test")

    async def temp_password(self, admin, temp_password, *, reason="new"):
        self.sent.append((f"temp_{reason}", admin.phone, temp_password))
        return SMSSendResult(success=True, provider_reference="test")

    async def phone_changed(self, old_phone, admin):
        self.sent.append(("phone_changed", old_phone, None))
        return SMSSendResult(success=True, provider_reference="test")

    def count(self, kind):
        return sum(1 for item in self.sent if item[0] == kind)

    def last(self, kind):
        return next(item for item in reversed(self.sent) if item[0] == kind)


@pytest_asyncio.fixture(loop_scope="module")
async def env(monkeypatch):
    outbox = Outbox()
    monkeypatch.setattr(account_routes, "send_otp_sms", outbox.otp)
    # lockout.send_lockout_notification and routes._notify_old_number both
    # import from this module at call time, so patching the attribute here
    # catches the background tasks too.
    monkeypatch.setattr(account_notifications, "send_lockout_sms", outbox.lockout_sms)
    monkeypatch.setattr(account_notifications, "send_phone_changed_sms", outbox.phone_changed)
    monkeypatch.setattr(account_notifications, "send_temp_password_sms", outbox.temp_password)

    # No Redis on this network. The limiter would fail open anyway, but it
    # would spend a connection timeout doing so on every single request.
    async def no_limit(key, bucket, limit=10, window_seconds=60):
        return None

    for module in (account_routes, auth_routes, auth_middleware):
        monkeypatch.setattr(module, "enforce_rate_limit", no_limit)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver/api/v1") as client:
        yield client, outbox


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def make_admin(*, pin: str | None = None, phone: str = PHONE) -> str:
    """A fresh operator + fully onboarded admin. Returns the admin's email."""
    slug = f"sec-{uuid.uuid4().hex[:10]}"
    email = f"{slug}@throwaway.test"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Sec {slug}", slug=slug, contact_email=f"{slug}@throwaway.test", status="approved")
        db.add(operator)
        await db.flush()
        db.add(
            AdminUser(
                isp_operator_id=operator.id,
                email=email,
                password_hash=hash_password(PASSWORD),
                role="superadmin",
                is_active=True,
                phone=phone,
                phone_verified=True,
                must_complete_onboarding=False,
                must_change_password=False,
                security_question="birth_town",
                security_answer_hash=hash_password("accra"),
                pin_hash=hash_password(pin) if pin else None,
                pin_set_at=datetime.now(timezone.utc) if pin else None,
            )
        )
        await db.commit()
    return email


async def row(email) -> "AdminUser":
    async with async_session_factory() as db:
        return (await db.execute(select(AdminUser).where(AdminUser.email == email))).scalar_one()


async def patch_admin(email, **values):
    """Write columns straight onto the admin — used to move a stored deadline
    into the past instead of sleeping."""
    async with async_session_factory() as db:
        admin = (await db.execute(select(AdminUser).where(AdminUser.email == email))).scalar_one()
        for key, value in values.items():
            setattr(admin, key, value)
        await db.commit()


async def events(email, event_type=None):
    async with async_session_factory() as db:
        admin = (await db.execute(select(AdminUser).where(AdminUser.email == email))).scalar_one()
        stmt = select(AdminSecurityEvent).where(AdminSecurityEvent.admin_user_id == admin.id)
        if event_type:
            stmt = stmt.where(AdminSecurityEvent.event_type == event_type)
        return (await db.execute(stmt.order_by(AdminSecurityEvent.created_at))).scalars().all()


async def login(client, email, password=PASSWORD):
    return await client.post("/auth/login", json={"email": email, "password": password})


async def elevated_token(client, email, pin=PIN):
    """Sign in and spend the PIN, returning a token good for the window."""
    token = (await login(client, email)).json()["access_token"]
    res = await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": pin})
    assert res.status_code == 200, res.text
    return token


# ── Login lockout ─────────────────────────────────────────────────────────


async def test_login_locks_at_exactly_five_failures(env):
    client, outbox = env
    email = await make_admin()

    for attempt in range(1, LOGIN_MAX_ATTEMPTS):
        res = await login(client, email, "WrongPassword1")
        assert res.status_code == 401
        assert res.json()["detail"] == auth_routes.INVALID_CREDENTIALS
        admin = await row(email)
        assert admin.login_attempt_count == attempt
        assert admin.login_locked_until is None, f"locked early at attempt {attempt}"

    # The correct password still works right up to the threshold.
    assert (await login(client, email)).status_code == 200
    assert (await row(email)).login_attempt_count == 0

    for _ in range(LOGIN_MAX_ATTEMPTS):
        assert (await login(client, email, "WrongPassword1")).status_code == 401

    admin = await row(email)
    assert admin.login_locked_until is not None
    remaining = admin.login_locked_until - datetime.now(timezone.utc)
    assert timedelta(hours=LOCKOUT_HOURS) - timedelta(minutes=1) < remaining <= timedelta(hours=LOCKOUT_HOURS)

    # Locked out even with the RIGHT password, and indistinguishable from a
    # wrong one — same status, same message.
    res = await login(client, email)
    assert res.status_code == 401
    assert res.json()["detail"] == auth_routes.INVALID_CREDENTIALS

    assert outbox.count("lockout_login") == 1
    assert outbox.last("lockout_login")[1] == PHONE
    assert len(await events(email, "login_lockout")) == 1


async def test_login_lockout_sms_is_not_repeated_while_locked(env):
    client, outbox = env
    email = await make_admin()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await login(client, email, "WrongPassword1")
    assert outbox.count("lockout_login") == 1

    # Ten more attempts, right and wrong, against a live lock.
    for _ in range(5):
        assert (await login(client, email, "WrongPassword1")).status_code == 401
        assert (await login(client, email)).status_code == 401

    assert outbox.count("lockout_login") == 1, "a live lock must not re-notify"
    assert len(await events(email, "login_lockout")) == 1
    # The counter must not have crept up either — a locked attempt is rejected
    # before it can count, which is what makes the suppression hold.
    assert (await row(email)).login_attempt_count == LOGIN_MAX_ATTEMPTS


async def test_lapsed_login_lock_starts_a_fresh_run(env):
    client, outbox = env
    email = await make_admin()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        await login(client, email, "WrongPassword1")
    await patch_admin(email, login_locked_until=datetime.now(timezone.utc) - timedelta(seconds=1))

    res = await login(client, email, "WrongPassword1")
    assert res.status_code == 401
    admin = await row(email)
    assert admin.login_attempt_count == 1, "a lapsed lock must not leave the counter at the threshold"
    assert admin.login_locked_until is None
    assert outbox.count("lockout_login") == 1  # still just the original


async def test_successful_login_clears_the_counter(env):
    client, _ = env
    email = await make_admin()
    for _ in range(LOGIN_MAX_ATTEMPTS - 1):
        await login(client, email, "WrongPassword1")
    assert (await row(email)).login_attempt_count == LOGIN_MAX_ATTEMPTS - 1

    assert (await login(client, email)).status_code == 200
    admin = await row(email)
    assert admin.login_attempt_count == 0
    assert admin.login_locked_until is None


async def test_unknown_email_is_indistinguishable(env):
    client, _ = env
    res = await login(client, "nobody-here@throwaway.test", "WrongPassword1")
    assert res.status_code == 401
    assert res.json()["detail"] == auth_routes.INVALID_CREDENTIALS


# ── PIN lockout ───────────────────────────────────────────────────────────


async def test_pin_locks_at_exactly_ten_failures(env):
    client, outbox = env
    email = await make_admin(pin=PIN)
    token = (await login(client, email)).json()["access_token"]
    before = (await row(email)).token_version

    for attempt in range(1, PIN_MAX_ATTEMPTS):
        res = await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": "0000"})
        assert res.status_code == 400, res.text
        assert f"{PIN_MAX_ATTEMPTS - attempt} attempts remaining" in res.json()["detail"]
        admin = await row(email)
        assert admin.pin_attempt_count == attempt
        assert admin.pin_locked_until is None, f"locked early at attempt {attempt}"
        assert admin.token_version == before, "sessions must survive until the lock"

    res = await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": "0000"})
    assert res.status_code == 403
    assert res.headers["X-Pin-Required"] == "locked"

    admin = await row(email)
    assert admin.pin_locked_until is not None
    remaining = admin.pin_locked_until - datetime.now(timezone.utc)
    assert timedelta(hours=LOCKOUT_HOURS) - timedelta(minutes=1) < remaining <= timedelta(hours=LOCKOUT_HOURS)
    # "Log the user out": token_version moved, so every issued token is dead.
    assert admin.token_version == before + 1
    assert admin.login_attempt_count == 0, "a PIN lockout must not touch the login counter"

    assert outbox.count("lockout_pin") == 1
    assert len(await events(email, "pin_lockout")) == 1

    # The old token is now refused by the ordinary auth guard, not the gate.
    assert (await client.get("/auth/me/security", headers=bearer(token))).status_code == 401


async def test_pin_lockout_sms_is_not_repeated_while_locked(env):
    client, outbox = env
    email = await make_admin(pin=PIN)
    token = (await login(client, email)).json()["access_token"]
    for _ in range(PIN_MAX_ATTEMPTS):
        await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": "0000"})
    assert outbox.count("lockout_pin") == 1

    fresh = (await login(client, email)).json()["access_token"]
    for _ in range(5):
        res = await client.post("/auth/me/pin/verify", headers=bearer(fresh), json={"pin": "0000"})
        assert res.status_code == 403
        res = await client.post("/auth/me/pin/verify", headers=bearer(fresh), json={"pin": PIN})
        assert res.status_code == 403, "even the right PIN is refused while locked"

    assert outbox.count("lockout_pin") == 1, "a live lock must not re-notify"
    assert len(await events(email, "pin_lockout")) == 1


async def test_correct_pin_clears_the_counter(env):
    client, _ = env
    email = await make_admin(pin=PIN)
    token = (await login(client, email)).json()["access_token"]
    for _ in range(PIN_MAX_ATTEMPTS - 1):
        await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": "0000"})
    assert (await row(email)).pin_attempt_count == PIN_MAX_ATTEMPTS - 1

    res = await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": PIN})
    assert res.status_code == 200
    assert (await row(email)).pin_attempt_count == 0


# ── Elevation window ──────────────────────────────────────────────────────


async def test_elevation_window_is_fifteen_minutes_and_expires(env):
    client, _ = env
    email = await make_admin(pin=PIN)
    token = await elevated_token(client, email)

    admin = await row(email)
    window = admin.pin_verified_until - datetime.now(timezone.utc)
    assert timedelta(minutes=PIN_ELEVATION_MINUTES) - timedelta(seconds=30) < window
    assert window <= timedelta(minutes=PIN_ELEVATION_MINUTES)

    # Inside the window a gated write is allowed through the gate.
    res = await client.post("/auth/me/phone", headers=bearer(token), json={"phone": PHONE})
    assert res.status_code == 200, res.text

    # One second before expiry: still in.
    await patch_admin(email, pin_verified_until=datetime.now(timezone.utc) + timedelta(seconds=1))
    res = await client.post("/auth/me/phone", headers=bearer(token), json={"phone": PHONE})
    assert res.status_code == 200, "elevation must hold right up to the deadline"

    # One second after: out, and it is the gate that says so.
    await patch_admin(email, pin_verified_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    res = await client.post("/auth/me/phone", headers=bearer(token), json={"phone": PHONE})
    assert res.status_code == 403
    assert res.headers["X-Pin-Required"] == "verify"

    # The window is fixed, not sliding: re-verifying is what re-opens it.
    res = await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": PIN})
    assert res.status_code == 200
    assert (await client.post("/auth/me/phone", headers=bearer(token), json={"phone": PHONE})).status_code == 200


async def test_elevation_does_not_slide_with_use(env):
    client, _ = env
    email = await make_admin(pin=PIN)
    token = await elevated_token(client, email)
    first = (await row(email)).pin_verified_until

    for _ in range(3):
        assert (await client.post("/auth/me/phone", headers=bearer(token), json={"phone": PHONE})).status_code == 200

    assert (await row(email)).pin_verified_until == first, "using the window must not extend it"


# ── Gated routes across every admin state ─────────────────────────────────


@pytest.mark.parametrize("method,path", GATED)
async def test_gated_routes_require_a_pin(env, method, path):
    client, _ = env
    email = await make_admin()  # no PIN at all
    token = (await login(client, email)).json()["access_token"]

    res = await client.request(method, path, headers=bearer(token), json={})
    assert res.status_code == 403
    assert res.headers["X-Pin-Required"] == "setup"


async def test_gate_reports_each_state(env):
    client, _ = env
    probe = ("POST", "/auth/me/security-question")

    # 1. no PIN on the account
    email = await make_admin()
    token = (await login(client, email)).json()["access_token"]
    res = await client.request(*probe, headers=bearer(token), json={})
    assert (res.status_code, res.headers["X-Pin-Required"]) == (403, "setup")

    # 2. PIN set, never verified this session
    email = await make_admin(pin=PIN)
    token = (await login(client, email)).json()["access_token"]
    res = await client.request(*probe, headers=bearer(token), json={})
    assert (res.status_code, res.headers["X-Pin-Required"]) == (403, "verify")

    # 3. elevation expired
    await patch_admin(email, pin_verified_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    res = await client.request(*probe, headers=bearer(token), json={})
    assert (res.status_code, res.headers["X-Pin-Required"]) == (403, "verify")

    # 4. locked
    await patch_admin(email, pin_locked_until=datetime.now(timezone.utc) + timedelta(hours=1))
    res = await client.request(*probe, headers=bearer(token), json={})
    assert (res.status_code, res.headers["X-Pin-Required"]) == (403, "locked")

    # 5. elevated — the gate is silent and the handler takes over
    await patch_admin(email, pin_locked_until=None)
    token = await elevated_token(client, email)
    res = await client.post(
        "/auth/me/security-question",
        headers=bearer(token),
        json={"current_password": PASSWORD, "security_question": "first_school", "security_answer": "Ridge"},
    )
    assert res.status_code == 200, res.text
    assert "X-Pin-Required" not in res.headers
    assert (await row(email)).security_question == "first_school"


async def test_reads_and_recovery_stay_open_without_a_pin(env):
    client, _ = env
    email = await make_admin()
    token = (await login(client, email)).json()["access_token"]

    for path in ("/auth/me/security", "/payment-credentials", "/sms-credentials"):
        res = await client.get(path, headers=bearer(token))
        assert res.status_code == 200, f"{path}: {res.text}"
        assert "X-Pin-Required" not in res.headers

    # Changing the password must never need the PIN, or a forgotten PIN and a
    # forgotten password would deadlock each other.
    res = await client.post(
        "/auth/me/password",
        headers=bearer(token),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
    )
    assert res.status_code == 200, res.text


async def test_first_pin_setup_is_not_gated_but_replacing_one_is(env):
    client, _ = env
    email = await make_admin()
    token = (await login(client, email)).json()["access_token"]

    res = await client.post(
        "/auth/me/pin",
        headers=bearer(token),
        json={"current_password": PASSWORD, "new_pin": PIN, "confirm_pin": PIN},
    )
    assert res.status_code == 200, res.text
    assert (await row(email)).pin_hash is not None
    assert [e.event_type for e in await events(email)] == ["pin_set"]

    # Setting it granted elevation, so an immediate change goes through.
    res = await client.post(
        "/auth/me/pin",
        headers=bearer(token),
        json={"current_password": PASSWORD, "current_pin": PIN, "new_pin": OTHER_PIN, "confirm_pin": OTHER_PIN},
    )
    assert res.status_code == 200, res.text

    # Once elevation lapses, replacing it is gated.
    await patch_admin(email, pin_verified_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    res = await client.post(
        "/auth/me/pin",
        headers=bearer(token),
        json={"current_password": PASSWORD, "current_pin": OTHER_PIN, "new_pin": PIN, "confirm_pin": PIN},
    )
    assert res.status_code == 403
    assert res.headers["X-Pin-Required"] == "verify"


async def test_forgotten_pin_can_be_reset_while_locked(env):
    client, outbox = env
    email = await make_admin(pin=PIN)
    token = (await login(client, email)).json()["access_token"]
    for _ in range(PIN_MAX_ATTEMPTS):
        await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": "0000"})
    assert (await row(email)).pin_locked_until is not None

    token = (await login(client, email)).json()["access_token"]  # token_version moved
    res = await client.post("/auth/me/pin/forgot", headers=bearer(token))
    assert res.status_code == 200, res.text
    code = outbox.last("pin_reset")[2]

    res = await client.post(
        "/auth/me/pin/reset",
        headers=bearer(token),
        json={"code": code, "current_password": PASSWORD, "new_pin": OTHER_PIN, "confirm_pin": OTHER_PIN},
    )
    assert res.status_code == 200, res.text

    admin = await row(email)
    assert admin.pin_locked_until is None, "resetting the PIN must clear the lock"
    assert admin.pin_attempt_count == 0
    assert (await client.post("/auth/me/phone", headers=bearer(token), json={"phone": PHONE})).status_code == 200


# ── Phone change quota ────────────────────────────────────────────────────


async def do_change(client, outbox, token, number):
    """Request + verify a number change, returning the verify response."""
    res = await client.post("/auth/me/phone", headers=bearer(token), json={"phone": number})
    if res.status_code != 200:
        return res
    code = outbox.last("phone_change")[2]
    return await client.post("/auth/me/phone/verify", headers=bearer(token), json={"code": code})


async def test_phone_change_quota_across_the_rolling_window(env):
    client, outbox = env
    email = await make_admin(pin=PIN)
    token = await elevated_token(client, email)

    numbers = ["233244000222", "233244000333", "233244000444"]
    for index, number in enumerate(numbers, start=1):
        res = await do_change(client, outbox, token, number)
        assert res.status_code == 200, res.text
        assert res.json()["changed"] is True
        assert res.json()["phone_changes_used"] == index
        assert (await row(email)).phone == number

    assert len(await events(email, "phone_changed")) == PHONE_CHANGE_LIMIT
    # The number being replaced is warned each time.
    assert outbox.count("phone_changed") == PHONE_CHANGE_LIMIT

    # Fourth change is refused, and refused at request time so no SMS is spent.
    sent_before = outbox.count("phone_change")
    res = await client.post("/auth/me/phone", headers=bearer(token), json={"phone": "233244000555"})
    assert res.status_code == 429
    assert str(PHONE_CHANGE_LIMIT) in res.json()["detail"]
    assert outbox.count("phone_change") == sent_before, "an over-quota request must not send a code"
    assert (await row(email)).phone == numbers[-1]

    # Ageing the oldest change out of the window frees exactly one slot.
    oldest = (await events(email, "phone_changed"))[0]
    async with async_session_factory() as db:
        stored = await db.get(AdminSecurityEvent, oldest.id)
        stored.created_at = datetime.now(timezone.utc) - timedelta(days=PHONE_CHANGE_WINDOW_DAYS + 1)
        await db.commit()

    res = await do_change(client, outbox, token, "233244000555")
    assert res.status_code == 200, res.text
    assert res.json()["phone_changes_used"] == PHONE_CHANGE_LIMIT

    # And the window closes again behind it.
    res = await client.post("/auth/me/phone", headers=bearer(token), json={"phone": "233244000666"})
    assert res.status_code == 429


async def test_reverifying_the_same_number_does_not_burn_quota(env):
    client, outbox = env
    email = await make_admin(pin=PIN)
    token = await elevated_token(client, email)

    for _ in range(PHONE_CHANGE_LIMIT + 2):
        res = await do_change(client, outbox, token, PHONE)
        assert res.status_code == 200, res.text
        assert res.json()["changed"] is False
        assert res.json()["phone_changes_used"] == 0

    assert await events(email, "phone_changed") == []
    assert len(await events(email, "phone_reverified")) == PHONE_CHANGE_LIMIT + 2
    assert outbox.count("phone_changed") == 0, "nothing changed, so the old number is not warned"

    # All three real changes are still available afterwards.
    for number in ("233244000777", "233244000888", "233244000999"):
        assert (await do_change(client, outbox, token, number)).status_code == 200
    res = await client.post("/auth/me/phone", headers=bearer(token), json={"phone": "233244001000"})
    assert res.status_code == 429


async def test_phone_change_quota_is_reported_before_it_is_spent(env):
    client, outbox = env
    email = await make_admin(pin=PIN)
    token = await elevated_token(client, email)

    status = (await client.get("/auth/me/security", headers=bearer(token))).json()
    assert status["phone_changes_used"] == 0
    assert status["phone_changes_limit"] == PHONE_CHANGE_LIMIT
    assert status["phone_change_window_resets_at"] is None

    await do_change(client, outbox, token, "233244001111")
    status = (await client.get("/auth/me/security", headers=bearer(token))).json()
    assert status["phone_changes_used"] == 1
    assert status["phone_change_window_resets_at"] is not None


# ── Password resets clear both lockouts ───────────────────────────────────


def locked_state():
    """Both counters sitting at their thresholds. A function, not a module-level
    dict: the constants come from the guarded import block, so evaluating this
    at import time would raise NameError during collection on a plain
    `pytest tests/` run instead of skipping cleanly."""
    return dict(login_attempt_count=LOGIN_MAX_ATTEMPTS, pin_attempt_count=PIN_MAX_ATTEMPTS)


async def assert_all_clear(email):
    admin = await row(email)
    assert admin.login_attempt_count == 0
    assert admin.login_locked_until is None
    assert admin.pin_attempt_count == 0
    assert admin.pin_locked_until is None
    return admin


async def test_self_service_password_change_clears_both_lockouts(env):
    client, _ = env
    email = await make_admin(pin=PIN)
    token = await elevated_token(client, email)
    locked_for = datetime.now(timezone.utc) + timedelta(hours=LOCKOUT_HOURS)
    await patch_admin(email, login_locked_until=locked_for, pin_locked_until=locked_for, **locked_state())

    res = await client.post(
        "/auth/me/password",
        headers=bearer(token),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
    )
    assert res.status_code == 200, res.text

    admin = await assert_all_clear(email)
    # Elevation is NOT inherited from a password change — the PIN still has to
    # be spent again.
    assert admin.pin_verified_until is None
    assert (await login(client, email, NEW_PASSWORD)).status_code == 200


async def test_reset_token_password_set_clears_both_lockouts(env):
    client, outbox = env
    email = await make_admin(pin=PIN)
    locked_for = datetime.now(timezone.utc) + timedelta(hours=LOCKOUT_HOURS)
    await patch_admin(email, login_locked_until=locked_for, pin_locked_until=locked_for, **locked_state())

    # Locked out of login entirely — the forgot-password route is the way back.
    assert (await login(client, email)).status_code == 401

    res = await client.post("/auth/reset/request", json={"email": email})
    assert res.status_code == 200, res.text
    code = outbox.last("reset")[2]
    res = await client.post("/auth/reset/verify-otp", json={"email": email, "code": code})
    assert res.status_code == 200, res.text
    grant = res.json()["reset_token"]

    res = await client.post(
        "/auth/reset/set-password",
        json={"reset_token": grant, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
    )
    assert res.status_code == 200, res.text

    await assert_all_clear(email)
    assert (await login(client, email, NEW_PASSWORD)).status_code == 200


async def test_platform_owner_reset_clears_both_lockouts(env):
    client, _ = env
    email = await make_admin(pin=PIN)
    locked_for = datetime.now(timezone.utc) + timedelta(hours=LOCKOUT_HOURS)
    await patch_admin(email, login_locked_until=locked_for, pin_locked_until=locked_for, **locked_state())

    async with async_session_factory() as db:
        owner = PlatformOwner(
            email=f"owner-{uuid.uuid4().hex[:8]}@throwaway.test",
            password_hash=hash_password(PASSWORD),
            name="Owner",
            is_active=True,
        )
        db.add(owner)
        await db.flush()
        admin = (await db.execute(select(AdminUser).where(AdminUser.email == email).with_for_update())).scalar_one()
        # reset_admin_password() commits; the temp-password SMS is patched out
        # at the notifications module, which is where platform_reset looks it up.
        await platform_reset.reset_admin_password(db, admin=admin, platform_owner_id=owner.id, phone=None)

    admin = await assert_all_clear(email)
    assert admin.pin_verified_until is None
    assert admin.must_change_password is True, "the reset still sends them to choose a password"


async def test_pin_survives_a_password_change(env):
    """The PIN is a separate secret: a password change clears the LOCK but must
    not silently discard the PIN itself."""
    client, _ = env
    email = await make_admin(pin=PIN)
    token = await elevated_token(client, email)
    before = (await row(email)).pin_hash

    res = await client.post(
        "/auth/me/password",
        headers=bearer(token),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
    )
    assert res.status_code == 200

    admin = await row(email)
    assert admin.pin_hash == before
    token = (await login(client, email, NEW_PASSWORD)).json()["access_token"]
    assert (await client.post("/auth/me/pin/verify", headers=bearer(token), json={"pin": PIN})).status_code == 200
