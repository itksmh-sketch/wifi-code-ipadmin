import string
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select
from src.db.models import Voucher, Plan, Site, Session, Router
from src.schemas import VoucherStatus, VoucherGenerate
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
    rows = (
        await db.execute(
            select(Voucher.code, Voucher.username).where(
                or_(Voucher.code.in_(candidates), Voucher.username.in_(candidates))
            )
        )
    ).all()
    taken = set()
    for code, username in rows:
        taken.add(code)
        taken.add(username)
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
        candidates = {generate_voucher_code(length) for _ in range(count - len(pool))} - pool
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
) -> Voucher:
    """Transition a voucher to a new status with strict state machine validation."""
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

    await db.commit()
    await db.refresh(voucher)
    return voucher


async def restore_voucher_status(
    db: AsyncSession,
    voucher_id: uuid.UUID,
    isp_operator_id: uuid.UUID,
) -> Voucher:
    """Re-enable a disabled voucher based on its activation and limit state."""
    result = await db.execute(select(Voucher).where(Voucher.id == voucher_id, Voucher.isp_operator_id == isp_operator_id))
    voucher = result.scalar_one_or_none()
    if not voucher:
        raise ValueError(f"Voucher {voucher_id} not found")
    if voucher.status != "disabled":
        raise ValueError(f"Invalid transition from '{voucher.status}' to restored state")

    if voucher.activated_at is None:
        return await transition_voucher_status(db, voucher_id, "unused", isp_operator_id)

    now = datetime.now(timezone.utc)
    expires_at = voucher.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            return await transition_voucher_status(db, voucher_id, "expired", isp_operator_id)

    plan_result = await db.execute(select(Plan).where(Plan.id == voucher.plan_id, Plan.isp_operator_id == isp_operator_id))
    plan = plan_result.scalar_one_or_none()
    if (
        plan
        and plan.data_limit_mb is not None
        and voucher.data_used_mb >= plan.data_limit_mb
    ):
        return await transition_voucher_status(db, voucher_id, "exhausted", isp_operator_id)

    return await transition_voucher_status(db, voucher_id, "active", isp_operator_id)


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

    voucher = await transition_voucher_status(db, voucher_id, "disabled", isp_operator_id)

    if not active_session:
        return voucher

    router = (
        await db.execute(select(Router).where(Router.id == active_session.router_id, Router.isp_operator_id == isp_operator_id))
    ).scalar_one_or_none()
    if not router:
        return voucher

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
        )
        db.add(voucher)
        vouchers.append(voucher)

    await db.commit()
    for v in vouchers:
        await db.refresh(v)

    return vouchers, batch_id
