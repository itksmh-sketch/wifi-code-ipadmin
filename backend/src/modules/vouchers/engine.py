import re
import string
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, or_, select, text
from src.db.models import Voucher, Plan, Site, Session, Router
from src.schemas import VoucherStatus, VoucherGenerate
from src.modules import audit
from src.modules.audit.fields import VOUCHER
from src.modules.audit.redact import mask_code
from src.modules.mikrotik.access_revocation import revoke_voucher_access
from src.radius.coa_events import create_pending_disconnect_event, send_disconnect_with_event

# Valid transitions for voucher lifecycle
VALID_TRANSITIONS = {
    ("unused", "active"),
    ("active", "exhausted"),
    ("active", "expired"),
    ("unused", "disabled"),
    ("active", "disabled"),
    ("exhausted", "disabled"),
    ("expired", "disabled"),
    ("disabled", "unused"),
    ("disabled", "active"),
    ("disabled", "expired"),
    ("disabled", "exhausted"),
}


# Voucher codes are drawn from 36 symbols (A-Z, 0-9) and printed in dash-separated
# groups of 4. Both the alphabet and the grouping are part of what operators print
# and customers type; only the length is selectable.
CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_GROUP_SIZE = 4
DEFAULT_CODE_LENGTH = 16

# MIN_CODE_LENGTH is a SECURITY FLOOR, not a formatting preference. A voucher code
# is a bearer credential: anyone holding it gets the internet access it paid for,
# and the captive portal will check any code handed to it. At 8 symbols the space
# is 36^8 = 2.8e12, so with ~1e6 live codes a blind guess lands on a valid one
# roughly once in 2.8e6 tries. Each symbol removed divides that by 36 -- at 6 it is
# ~1 in 2,200, which is trivially brute-forceable against the portal. Do not lower
# this without redoing that arithmetic against the live voucher count and adding
# portal-side rate limiting to match. Above 24 the code stops being something a
# customer will retype off a printed slip.
MIN_CODE_LENGTH = 8
MAX_CODE_LENGTH = 24

# Refuse a batch that would claim more than this share of a length's code space in
# one go. Past it, retries stop being rare and the batch crawls instead of failing
# honestly.
MAX_BATCH_SPACE_FRACTION = 0.01

# Each round regenerates only the candidates that actually collided, so needing
# more than this many rounds means the space is saturated, not that we were
# unlucky. Bounds the loop so a full code space errors instead of spinning.
MAX_COLLISION_ROUNDS = 10


def generate_voucher_code(length: int = DEFAULT_CODE_LENGTH) -> str:
    """Generate a voucher code: `length` uppercase alphanumeric characters, printed
    in dash-separated groups of 4 (the last group is short when `length` is not a
    multiple of 4). Uses `secrets` rather than `random`: a voucher is bearer credit,
    so codes must not be predictable from earlier ones."""
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
    return "-".join(body[i:i + CODE_GROUP_SIZE] for i in range(0, length, CODE_GROUP_SIZE))


def generate_voucher_username(length: int = DEFAULT_CODE_LENGTH) -> str:
    """Generate a RADIUS username. Same shape as a code -- authorize_check_query
    matches either column against SQL-User-Name."""
    return generate_voucher_code(length)


def validate_code_length(length: int) -> None:
    """Raise if `length` is outside the supported bounds."""
    if not isinstance(length, int) or isinstance(length, bool):
        raise ValueError("Voucher code length must be a whole number")
    # The lower bound is a security limit, not an arbitrary one: below
    # MIN_CODE_LENGTH a voucher code -- a bearer credential -- becomes guessable by
    # brute force against the captive portal. See the constant for the arithmetic.
    # The upper bound is ergonomic (a customer has to retype it off a printed slip).
    if length < MIN_CODE_LENGTH or length > MAX_CODE_LENGTH:
        raise ValueError(
            f"Voucher code length must be between {MIN_CODE_LENGTH} and {MAX_CODE_LENGTH} characters"
        )


def max_codes_per_batch(length: int) -> int:
    """How many codes may be minted in one batch at this length before the space is
    crowded enough that regeneration stops being cheap."""
    return int(len(CODE_ALPHABET) ** length * MAX_BATCH_SPACE_FRACTION)


# ── code_key and the numeric reseller codes (migrations 061/062) ──────────

_NOT_KEY = re.compile(r"[^A-Za-z0-9]")


def code_key(text: str | None) -> str:
    """The code as the captive portal matches it: ASCII letters and digits
    only, upper-cased, so "abcd efgh-jklm" and "ABCDEFGHJKLM" are one code.
    The same expression as the vouchers.code_key generated column; a test
    runs both on the same inputs."""
    return _NOT_KEY.sub("", text or "").upper()


# Damm's order-10 quasigroup: one check digit that catches every single-digit
# mistake and every swap of two neighbouring digits.
_DAMM = (
    (0, 3, 1, 7, 5, 9, 8, 6, 4, 2), (7, 0, 9, 2, 1, 5, 4, 8, 6, 3), (4, 2, 0, 6, 8, 7, 1, 3, 5, 9),
    (1, 7, 5, 0, 9, 8, 3, 4, 2, 6), (6, 1, 2, 3, 0, 4, 5, 9, 7, 8), (3, 6, 7, 4, 2, 0, 9, 5, 8, 1),
    (5, 8, 6, 9, 7, 2, 0, 1, 3, 4), (8, 9, 4, 5, 3, 6, 2, 0, 1, 7), (9, 4, 3, 8, 6, 1, 7, 2, 0, 5),
    (2, 5, 8, 1, 4, 3, 6, 7, 9, 0),
)


def _damm_interim(digits: str) -> int:
    interim = 0
    for ch in digits:
        interim = _DAMM[interim][ord(ch) - 48]
    return interim


def damm_check_digit(digits: str) -> str:
    """The digit to append to ``digits`` (ASCII 0-9 only)."""
    if not (digits.isascii() and digits.isdigit()):
        raise ValueError("Damm check digit needs ASCII digits")
    return str(_damm_interim(digits))


def damm_valid(digits: str) -> bool:
    """Whether ``digits`` ends in its own Damm check digit."""
    return digits.isascii() and digits.isdigit() and _damm_interim(digits) == 0


# Reseller codes are digits: easier to read out over the phone and to type on
# a phone keypad. N-1 random digits and a Damm check digit. 12 digits while an
# operator has up to 10,000 live numeric codes, one more digit per tenfold
# after that, so a guesser who computes the check digit hits a live code with
# probability at most 10,000 / 10^11 = 1e-7 per try, at every size. The
# captive portal's failure limits (src.portal.login_limits) bound the tries.
NUMERIC_CODE_MIN_LENGTH = 12
NUMERIC_CODE_MAX_LENGTH = 16
NUMERIC_LIVE_CODES_AT_MIN_LENGTH = 10_000
LIVE_STATUSES = ("unused", "active")


def numeric_code_length(live_codes: int) -> int:
    """Digits (check digit included) for a batch, given the operator's live
    numeric codes counting the batch itself. Raises past the 16-digit cap."""
    length, cap = NUMERIC_CODE_MIN_LENGTH, NUMERIC_LIVE_CODES_AT_MIN_LENGTH
    while live_codes > cap:
        length, cap = length + 1, cap * 10
    if length > NUMERIC_CODE_MAX_LENGTH:
        raise ValueError(f"{live_codes} live numeric codes is past the {NUMERIC_CODE_MAX_LENGTH}-digit cap")
    return length


def is_numeric_code_key(key: str) -> bool:
    """A key shaped like a numeric reseller code: 12 to 16 ASCII digits. Only
    numeric codes are all digits (generate_unique_codes never makes one)."""
    return key.isascii() and key.isdigit() and NUMERIC_CODE_MIN_LENGTH <= len(key) <= NUMERIC_CODE_MAX_LENGTH


def generate_numeric_code(length: int) -> str:
    """``length`` digits, the last a Damm check digit, in dash-separated groups
    of 4 like every other code. Leading zeros are fine: it is text."""
    if not NUMERIC_CODE_MIN_LENGTH <= length <= NUMERIC_CODE_MAX_LENGTH:
        raise ValueError(f"Numeric code length must be {NUMERIC_CODE_MIN_LENGTH} to {NUMERIC_CODE_MAX_LENGTH}")
    body = "".join(str(secrets.randbelow(10)) for _ in range(length - 1))
    digits = body + damm_check_digit(body)
    return "-".join(digits[i:i + CODE_GROUP_SIZE] for i in range(0, length, CODE_GROUP_SIZE))


async def live_numeric_codes(db: AsyncSession, operator_id: uuid.UUID) -> int:
    """The operator's numeric codes still able to log in (unused or active)."""
    return (
        await db.execute(
            select(func.count()).select_from(Voucher).where(
                Voucher.isp_operator_id == operator_id,
                Voucher.status.in_(LIVE_STATUSES),
                Voucher.code_key.op("~")("^[0-9]+$"),
            )
        )
    ).scalar_one()


async def generate_numeric_codes(db: AsyncSession, operator_id: uuid.UUID, count: int) -> List[str]:
    """``count`` new numeric codes for the operator, sized by numeric_code_length.

    Takes a transaction-scoped advisory lock on the operator first, so two
    allocations can't both count 9,999 live codes and both choose 12 digits;
    the second waits for the first to commit. The caller's transaction holds
    it until commit or rollback.
    """
    if count < 1:
        raise ValueError("Generate at least one code")
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('numeric_codes:' || :op, 0))"),
        {"op": str(operator_id)},
    )
    length = numeric_code_length(await live_numeric_codes(db, operator_id) + count)
    pool: set = set()
    for _ in range(MAX_COLLISION_ROUNDS):
        if len(pool) >= count:
            break
        candidates = {generate_numeric_code(length) for _ in range(count - len(pool))} - pool
        pool |= candidates - await _taken_strings(db, candidates)
    if len(pool) < count:
        raise ValueError(f"Could not find {count} unused {length}-digit codes after {MAX_COLLISION_ROUNDS} attempts")
    return list(pool)[:count]


async def _taken_strings(db: AsyncSession, candidates: set) -> set:
    """Return the candidates already in use as either a code or a username.

    The uniqueness scope is GLOBAL, not per-operator: vouchers.code and
    vouchers.username each carry their own platform-wide UNIQUE constraint
    (vouchers_code_key / vouchers_username_key). Scoping this lookup to the
    operator would pass here and then fail on INSERT with an IntegrityError.
    Both columns are checked against every candidate so a code can never
    duplicate another voucher's username either -- authorize_check_query matches
    `v.code = ... OR v.username = ...`, and an overlap would make that ambiguous.
    """
    if not candidates:
        return set()
    # code_key is unique too (migration 061): a candidate whose key an existing
    # code already has is taken, even if it differs in dashes or case.
    by_key = {code_key(c): c for c in candidates}
    rows = (
        await db.execute(
            select(Voucher.code, Voucher.username, Voucher.code_key).where(
                or_(Voucher.code.in_(candidates), Voucher.username.in_(candidates), Voucher.code_key.in_(by_key))
            )
        )
    ).all()
    taken = set()
    for code, username, key in rows:
        taken.add(code)
        taken.add(username)
        if key in by_key:
            taken.add(by_key[key])
    return taken


async def generate_unique_codes(
    db: AsyncSession, count: int, length: int = DEFAULT_CODE_LENGTH
) -> List[str]:
    """Mint `count` codes of `length` symbols, none of which collide with each other
    or with any code/username already stored.

    One membership query per round rather than one per code, so a 500-voucher batch
    costs a single round trip in the common case where nothing collides.
    """
    validate_code_length(length)
    if count < 0:
        raise ValueError("Cannot generate a negative number of codes")
    limit = max_codes_per_batch(length)
    if count > limit:
        raise ValueError(
            f"{count} codes do not fit safely in the {length}-character code space "
            f"(limit {limit}); choose a longer code length"
        )

    pool: set = set()
    for _ in range(MAX_COLLISION_ROUNDS):
        if len(pool) >= count:
            break
        # A set comprehension drops duplicates drawn within the round; subtracting
        # `pool` drops ones already accepted in an earlier round. Neither is visible
        # to the DB yet -- the batch is not flushed until commit -- so in-batch
        # collisions have to be caught here.
        # All-digit keys are kept for numeric reseller codes, so the portal can
        # check a numeric code's Damm digit before looking it up. A random
        # 8-24 symbol code is all digits about once in 3e4 at 8, never in
        # practice at 16; dropping one costs nothing.
        candidates = {
            c for c in (generate_voucher_code(length) for _ in range(count - len(pool))) if not code_key(c).isdigit()
        } - pool
        pool |= candidates - await _taken_strings(db, candidates)

    if len(pool) < count:
        raise ValueError(
            f"Could not find {count} unused {length}-character codes after "
            f"{MAX_COLLISION_ROUNDS} attempts; choose a longer code length"
        )
    return list(pool)[:count]


def generate_voucher_password() -> str:
    """Generate a random password for RADIUS authentication."""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(12))


def validate_transition(current_status: str, new_status: str) -> bool:
    """Check if a status transition is valid."""
    return (current_status, new_status) in VALID_TRANSITIONS


async def transition_voucher_status(
    db: AsyncSession,
    voucher_id: uuid.UUID,
    new_status: str,
    isp_operator_id: uuid.UUID,
    *,
    commit: bool = True,
) -> Voucher:
    """Transition a voucher to a new status with strict state machine validation.

    ``commit=False`` flushes instead, for a caller that adds its audit row to
    the same transaction.
    """
    result = await db.execute(select(Voucher).where(Voucher.id == voucher_id, Voucher.isp_operator_id == isp_operator_id))
    voucher = result.scalar_one_or_none()
    if not voucher:
        raise ValueError(f"Voucher {voucher_id} not found")

    if not validate_transition(voucher.status, new_status):
        raise ValueError(
            f"Invalid transition from '{voucher.status}' to '{new_status}'"
        )

    voucher.status = new_status

    if new_status == "active" and voucher.activated_at is None:
        voucher.activated_at = datetime.now(timezone.utc)
        # Start expiry on first successful use, not at generation time.
        plan_result = await db.execute(select(Plan).where(Plan.id == voucher.plan_id, Plan.isp_operator_id == isp_operator_id))
        plan = plan_result.scalar_one_or_none()
        if plan and plan.duration_minutes is not None:
            voucher.expires_at = voucher.activated_at + timedelta(minutes=plan.duration_minutes)

    if not commit:
        await db.flush()
        return voucher
    await db.commit()
    await db.refresh(voucher)
    return voucher


_AUTO_ACTIONS = {"expired": "voucher.expire", "exhausted": "voucher.exhaust"}


async def end_voucher_automatically(
    db: AsyncSession, voucher_id: uuid.UUID, new_status: str, isp_operator_id: uuid.UUID, *, via: str,
) -> Voucher:
    """Expire or exhaust a voucher on the system's own decision (time or data
    used up), with its audit row in the same commit. ``via`` says what noticed:
    the expiry job or RADIUS accounting. The voucher only as its masked
    suffix: a code is bearer credit. The row fails open: the voucher ends
    whether or not the audit write succeeds."""
    old_status = (
        await db.execute(select(Voucher.status).where(Voucher.id == voucher_id, Voucher.isp_operator_id == isp_operator_id))
    ).scalar_one_or_none()
    voucher = await transition_voucher_status(db, voucher_id, new_status, isp_operator_id, commit=False)
    await audit.record_system_fail_open(
        db, _AUTO_ACTIONS[new_status], isp_operator_id=isp_operator_id, target_type="voucher", target_id=voucher.id,
        summary=f"Voucher {mask_code(voucher.code)} {new_status}" + (" (data used up)" if new_status == "exhausted" else ""),
        changes={"status": {"before": old_status, "after": new_status}},
        detail={"code_suffix": mask_code(voucher.code), "plan_id": voucher.plan_id, "via": via},
    )
    await db.commit()
    await db.refresh(voucher)
    return voucher


async def restore_voucher_status(
    db: AsyncSession,
    voucher_id: uuid.UUID,
    isp_operator_id: uuid.UUID,
) -> Voucher:
    """Re-enable a disabled voucher based on its activation and limit state.

    The three operator entry points (restore, disable, generate) write their
    audit rows here, in the transaction that makes the change; the shared
    transition above stays unaudited for the RADIUS and expiry paths.
    """
    result = await db.execute(select(Voucher).where(Voucher.id == voucher_id, Voucher.isp_operator_id == isp_operator_id))
    voucher = result.scalar_one_or_none()
    if not voucher:
        raise ValueError(f"Voucher {voucher_id} not found")
    if voucher.status != "disabled":
        raise ValueError(f"Invalid transition from '{voucher.status}' to restored state")

    before = VOUCHER.snapshot(voucher)
    voucher = await transition_voucher_status(
        db, voucher_id, await _restored_status(db, voucher, isp_operator_id), isp_operator_id, commit=False
    )
    audit.record(
        db, "voucher.reactivate", target_type="voucher", target_id=voucher.id,
        summary=f"Reactivated voucher {mask_code(voucher.code)} ({voucher.status})",
        changes=VOUCHER.diff(before, VOUCHER.snapshot(voucher)),
        detail={"code_suffix": mask_code(voucher.code), "plan_id": voucher.plan_id, "batch_id": voucher.batch_id},
    )
    await db.commit()
    await db.refresh(voucher)
    return voucher


async def _restored_status(db: AsyncSession, voucher: Voucher, isp_operator_id: uuid.UUID) -> str:
    if voucher.activated_at is None:
        return "unused"

    now = datetime.now(timezone.utc)
    expires_at = voucher.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            return "expired"

    plan_result = await db.execute(select(Plan).where(Plan.id == voucher.plan_id, Plan.isp_operator_id == isp_operator_id))
    plan = plan_result.scalar_one_or_none()
    if (
        plan
        and plan.data_limit_mb is not None
        and voucher.data_used_mb >= plan.data_limit_mb
    ):
        return "exhausted"

    return "active"


async def disable_voucher_with_disconnect(
    db: AsyncSession,
    voucher_id: uuid.UUID,
    isp_operator_id: uuid.UUID,
) -> Voucher:
    """Disable a voucher and disconnect any live session."""
    active_session_result = await db.execute(
        select(Session)
        .where(
            Session.voucher_id == voucher_id,
            Session.isp_operator_id == isp_operator_id,
            Session.stopped_at.is_(None),
        )
        .order_by(Session.started_at.desc())
        .limit(1)
    )
    active_session = active_session_result.scalar_one_or_none()

    voucher = (
        await db.execute(select(Voucher).where(Voucher.id == voucher_id, Voucher.isp_operator_id == isp_operator_id))
    ).scalar_one_or_none()
    before = VOUCHER.snapshot(voucher)
    voucher = await transition_voucher_status(db, voucher_id, "disabled", isp_operator_id, commit=False)
    audit.record(
        db, "voucher.disable", target_type="voucher", target_id=voucher.id,
        summary=f"Disabled voucher {mask_code(voucher.code)}" + (" and disconnected its session" if active_session else ""),
        changes=VOUCHER.diff(before, VOUCHER.snapshot(voucher)),
        detail={
            "code_suffix": mask_code(voucher.code), "plan_id": voucher.plan_id, "batch_id": voucher.batch_id,
            "had_live_session": bool(active_session),
        },
    )
    # Committed before the disconnect, as before: a client that reconnects at
    # once must already be refused.
    await db.commit()
    await db.refresh(voucher)

    router = None
    if active_session:
        router = (
            await db.execute(select(Router).where(Router.id == active_session.router_id, Router.isp_operator_id == isp_operator_id))
        ).scalar_one_or_none()
    if active_session and router:
        event = await create_pending_disconnect_event(
            db,
            isp_operator_id=isp_operator_id,
            voucher_id=voucher.id,
            router_id=router.id,
            session_row_id=active_session.id,
        )
        await send_disconnect_with_event(
            db,
            event=event,
            router=router,
            voucher=voucher,
            session=active_session,
        )

    # Router-side cleanup AFTER the CoA (running it first made every CoA NAK
    # because the session was already gone): removes any active entry the CoA
    # didn't reach — including a client with no session row at all — and
    # clears stored hotspot cookies.
    await revoke_voucher_access(db, voucher)

    await db.commit()
    await db.refresh(voucher)
    return voucher


async def generate_vouchers(
    db: AsyncSession,
    body: VoucherGenerate,
    isp_operator_id: uuid.UUID,
) -> Tuple[List[Voucher], str]:
    """Generate a batch of vouchers."""
    # Verify plan exists
    plan_result = await db.execute(
        select(Plan).where(Plan.id == body.plan_id, Plan.isp_operator_id == isp_operator_id, Plan.is_active == True)
    )
    plan = plan_result.scalar_one_or_none()
    if not plan:
        raise ValueError("Plan not found or is inactive")

    if body.site_id:
        site_result = await db.execute(select(Site).where(Site.id == body.site_id, Site.isp_operator_id == isp_operator_id))
        if not site_result.scalar_one_or_none():
            raise ValueError("Site not found")

    code_length = body.code_length if body.code_length is not None else DEFAULT_CODE_LENGTH
    validate_code_length(code_length)

    # Two strings per voucher: the printed code and the RADIUS username. They are
    # drawn from one pool so a code can never equal another voucher's username.
    needed = body.quantity * 2
    limit = max_codes_per_batch(code_length)
    if needed > limit:
        raise ValueError(
            f"A batch of {body.quantity} vouchers does not fit safely in the "
            f"{code_length}-character code space (max {limit // 2} per batch at this "
            f"length); choose a longer code length"
        )
    strings = await generate_unique_codes(db, needed, code_length)

    batch_id = str(uuid.uuid4())[:8]
    vouchers = []

    for i in range(body.quantity):
        code = strings[2 * i]
        username = strings[2 * i + 1]
        password = generate_voucher_password()

        voucher = Voucher(
            plan_id=body.plan_id,
            site_id=body.site_id,
            isp_operator_id=isp_operator_id,
            code=code,
            username=username,
            password=password,
            status="unused",
            device_policy=body.device_policy.value,
            max_devices=1,
            expires_at=None,
            batch_id=batch_id,
            source="manual",
        )
        db.add(voucher)
        vouchers.append(voucher)

    audit.record(
        db, "voucher.generate", target_type="voucher_batch", target_id=batch_id,
        summary=f"Generated {body.quantity} voucher(s) for plan {plan.name}",
        detail={
            "plan_id": plan.id, "plan_name": plan.name, "quantity": body.quantity, "site_id": body.site_id,
            "characters": code_length, "device_policy": body.device_policy.value,
        },
    )
    await db.commit()
    for v in vouchers:
        await db.refresh(v)

    return vouchers, batch_id
