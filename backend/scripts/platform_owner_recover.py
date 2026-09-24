"""Break-glass recovery for the platform owner account.

For when the owner cannot get themselves back in through the normal paths:
a lock they can't wait out, a security setup that went wrong, or a lost
character code with the SMS channel itself broken. It works directly on the
database, so it only runs inside the backend container:

    docker exec -it hotspot-backend python -m scripts.platform_owner_recover status
    docker exec -it hotspot-backend python -m scripts.platform_owner_recover status --email owner@example.com
    docker exec -it hotspot-backend python -m scripts.platform_owner_recover clear-login-lock --email owner@example.com
    docker exec -it hotspot-backend python -m scripts.platform_owner_recover clear-challenge-lock --email owner@example.com
    docker exec -it hotspot-backend python -m scripts.platform_owner_recover reset-setup --email owner@example.com

Add --dry-run to any action to see exactly what would change without changing
anything. Add --no-sms to skip the alert text to the owner's verified phone.

Safety, because there is no undo:
  * nothing changes without a typed confirmation: the action name and the
    owner's email, exactly as printed (e.g. "reset-setup owner@example.com").
    There is no flag that skips it. Piped input works (docker exec -i), so it
    can be scripted, but the phrase still has to be supplied;
  * the current state and the exact field-by-field change are printed first;
  * after confirmation the row is re-read under a lock, and if anything about
    it changed while the prompt was waiting, nothing is written;
  * every change writes a platform_owner_security_events row ("break_glass")
    and texts the owner's verified phone (best effort: SMS being broken may be
    why this is being run, and a failed send never blocks the recovery);
  * it prints nothing secret: no hashes, no code, no answer, phone masked.

What each action does:
  clear-login-lock      password lockout counter and lock -> cleared.
  clear-challenge-lock  character-code lockout counter and lock -> cleared, and
                        any outstanding challenge token voided. The positions
                        being asked stay the same (no fresh draw).
  reset-setup           back to "security setup required", from scratch: phone,
                        security question and character code are all removed,
                        both lockouts cleared, open OTP codes voided, and every
                        session signed out. The password is NOT changed. Next
                        sign-in is password-only and leads to the setup wizard.

Exit codes: 0 done (or nothing to do / dry run), 1 aborted, 2 usage error or
no such owner.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Runnable as `python -m scripts.platform_owner_recover` (from /app) or as a
# path; either way the app package must be importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, update  # noqa: E402

from src.db.base import async_session_factory  # noqa: E402
from src.db.models import PlatformOwner, PlatformOwnerOtpCode, PlatformOwnerSecurityEvent  # noqa: E402
from src.utils.phone import mask_phone  # noqa: E402

ACTIONS = ("status", "clear-login-lock", "clear-challenge-lock", "reset-setup")
ACTION_TEXT = {
    "clear-login-lock": "the sign-in lock was cleared",
    "clear-challenge-lock": "the character-code lock was cleared",
    "reset-setup": "security setup was reset (phone, question and code removed, all sessions signed out)",
}


@dataclass(frozen=True)
class Change:
    field: str
    before: str
    after: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _when(value: datetime | None) -> str:
    if value is None:
        return "none"
    state = "ACTIVE" if value > _now() else "lapsed"
    return f"{value.isoformat(timespec='seconds')} ({state})"


def describe(owner: PlatformOwner) -> list[tuple[str, str]]:
    """Display-safe state: never a hash, code, answer or full phone."""
    code = owner.challenge_hashes
    return [
        ("email", owner.email),
        ("active", str(owner.is_active)),
        ("security setup required", str(owner.must_complete_security_setup)),
        ("phone", f"{mask_phone(owner.phone) if owner.phone else 'none'} (verified={owner.phone_verified})"),
        ("security question", owner.security_question or "none"),
        ("character code", "none" if code is None else ("set, confirmed" if code.get("confirmed_at") else "set")),
        ("password failures / lock", f"{owner.login_attempt_count} / {_when(owner.login_locked_until)}"),
        ("code failures / lock", f"{owner.challenge_attempt_count} / {_when(owner.challenge_locked_until)}"),
        ("pending challenge", "yes" if owner.challenge_pending_jti else "no"),
        ("session version", str(owner.token_version)),
        ("last sign-in", owner.last_login_at.isoformat(timespec="seconds") if owner.last_login_at else "never"),
    ]


def plan(owner: PlatformOwner, action: str) -> list[Change]:
    """The exact changes ``action`` would make to this row. Empty = nothing to do."""
    changes: list[Change] = []

    def want(field: str, target, shown_before=None, shown_after=None):
        current = getattr(owner, field)
        if current != target:
            changes.append(Change(
                field,
                shown_before if shown_before is not None else ("none" if current is None else str(current)),
                shown_after if shown_after is not None else ("none" if target is None else str(target)),
            ))

    def clear_login():
        want("login_attempt_count", 0)
        want("login_locked_until", None)

    def clear_challenge():
        want("challenge_attempt_count", 0)
        want("challenge_locked_until", None)
        want("challenge_pending_jti", None, shown_before="(outstanding token)", shown_after="none")

    if action == "clear-login-lock":
        clear_login()
    elif action == "clear-challenge-lock":
        clear_challenge()
    elif action == "reset-setup":
        want("must_complete_security_setup", True)
        want("phone", None, shown_before=mask_phone(owner.phone) if owner.phone else None)
        want("phone_verified", False)
        want("security_question", None)
        want("security_answer_hash", None, shown_before="(set)")
        want("security_answer_attempt_count", 0)
        want("challenge_hashes", None, shown_before="(code on file)")
        want("challenge_set_at", None)
        want("challenge_pending_positions", None)
        clear_challenge()
        clear_login()
        # Always: everyone is signed out, whatever else was already clear.
        changes.append(Change("token_version", str(owner.token_version), f"{owner.token_version + 1} (all sessions signed out)"))
    return changes


def apply(owner: PlatformOwner, action: str) -> None:
    if action in ("clear-login-lock", "reset-setup"):
        owner.login_attempt_count = 0
        owner.login_locked_until = None
    if action in ("clear-challenge-lock", "reset-setup"):
        owner.challenge_attempt_count = 0
        owner.challenge_locked_until = None
        owner.challenge_pending_jti = None
    if action == "reset-setup":
        owner.must_complete_security_setup = True
        owner.phone = None
        owner.phone_verified = False
        owner.security_question = None
        owner.security_answer_hash = None
        owner.security_answer_attempt_count = 0
        owner.challenge_hashes = None
        owner.challenge_set_at = None
        owner.challenge_pending_positions = None
        owner.token_version = int(owner.token_version or 0) + 1


def print_state(owner: PlatformOwner, title: str) -> None:
    print(f"\n{title}")
    for label, value in describe(owner):
        print(f"  {label:<26} {value}")


async def find_owner(db, email: str, *, lock: bool = False) -> PlatformOwner | None:
    stmt = select(PlatformOwner).where(func.lower(PlatformOwner.email) == email.strip().lower())
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def notification_sms_configured() -> bool:
    from src.modules.platform.notification_sms_credentials_service import get_active_credential

    async with async_session_factory() as db:
        return await get_active_credential(db) is not None


def read_confirmation(phrase: str) -> bool:
    print(f"\nTo go ahead, type exactly:  {phrase}")
    try:
        typed = input("> " if sys.stdin.isatty() else "")
    except EOFError:
        print("\nNo confirmation received. Nothing was changed.")
        return False
    if typed.strip() != phrase:
        print("That does not match. Nothing was changed.")
        return False
    return True


async def run(args) -> int:
    if args.action == "status":
        async with async_session_factory() as db:
            if args.email:
                owner = await find_owner(db, args.email)
                if owner is None:
                    print(f"No platform owner with email {args.email!r}.")
                    return 2
                owners = [owner]
            else:
                owners = (await db.execute(select(PlatformOwner).order_by(PlatformOwner.created_at))).scalars().all()
        for owner in owners:
            print_state(owner, f"Platform owner {owner.email}")
        return 0

    async with async_session_factory() as db:
        owner = await find_owner(db, args.email)
    if owner is None:
        print(f"No platform owner with email {args.email!r}. Nothing was changed.")
        return 2
    if not owner.is_active:
        print(f"Platform owner {owner.email} is deactivated; this tool only recovers active accounts. Nothing was changed.")
        return 2

    print_state(owner, f"Current state of platform owner {owner.email}")
    changes = plan(owner, args.action)
    if not changes:
        print(f"\nNothing to change: {args.action} would leave this account exactly as it is.")
        return 0

    print(f"\n{args.action} will make these changes:")
    for c in changes:
        print(f"  {c.field:<30} {c.before}  ->  {c.after}")
    if args.action == "reset-setup":
        print("\nAfter this, the owner signs in with their password (unchanged) and must redo")
        print("security setup from the start: verify a phone by SMS, choose a security question,")
        print("and record a new character code.")
        if not await notification_sms_configured():
            print("\n!! WARNING: no platform notification SMS account is active. The owner will not be")
            print("!! able to verify a phone, so setup cannot be completed until SMS is fixed.")
    if args.dry_run:
        print("\n--dry-run: nothing was changed.")
        return 0

    phrase = f"{args.action} {owner.email}"
    if not read_confirmation(phrase):
        return 1

    alert_phone = owner.phone if (owner.phone_verified and owner.phone) else None
    async with async_session_factory() as db:
        fresh = await find_owner(db, args.email, lock=True)
        if fresh is None or fresh.id != owner.id or not fresh.is_active:
            print("The account changed while waiting for confirmation. Nothing was changed; run it again.")
            return 1
        if plan(fresh, args.action) != changes:
            print("The account's state changed while waiting for confirmation. Nothing was changed; run it again.")
            return 1
        apply(fresh, args.action)
        voided = 0
        if args.action == "reset-setup":
            result = await db.execute(
                update(PlatformOwnerOtpCode)
                .where(PlatformOwnerOtpCode.platform_owner_id == fresh.id, PlatformOwnerOtpCode.consumed_at.is_(None))
                .values(consumed_at=_now())
            )
            voided = result.rowcount or 0
        event = PlatformOwnerSecurityEvent(
            platform_owner_id=fresh.id,
            event_type="break_glass",
            detail={
                "action": args.action,
                "fields": [c.field for c in changes],
                "otp_codes_voided": voided,
                "run_by": _whoami(),
                "host": socket.gethostname(),
            },
        )
        db.add(event)
        await db.commit()
        event_id = event.id
        after = fresh
    print_state(after, f"Done. New state of platform owner {after.email}")
    if voided:
        print(f"  (voided {voided} open OTP code(s))")

    if args.no_sms:
        print("\n--no-sms: owner not texted.")
    elif alert_phone is None:
        print("\nNo verified phone on file (before this change), so no alert text was sent.")
    else:
        await _alert(alert_phone, args.action, event_id)
    return 0


def _whoami() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or "unknown"


async def _alert(phone: str, action: str, event_id) -> None:
    from src.modules.admin_accounts.notifications import send_platform_owner_break_glass_sms

    try:
        result = await send_platform_owner_break_glass_sms(phone, action_text=ACTION_TEXT[action])
    except Exception as exc:  # never let a notification failure look like a failed recovery
        result = None
        error = f"exception: {exc}"
    else:
        error = None if result.success else (result.error or "unknown")
    async with async_session_factory() as db:
        event = await db.get(PlatformOwnerSecurityEvent, event_id)
        if event is not None:
            event.sms_sent = bool(result and result.success)
            event.sms_error = error
            await db.commit()
    if result and result.success:
        print(f"\nAlert text sent to {mask_phone(phone)}.")
    else:
        print(f"\nAlert text to {mask_phone(phone)} could NOT be sent ({error}). The recovery itself is done.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.platform_owner_recover",
        description="Break-glass recovery for the platform owner account. Run inside the backend container.",
    )
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--email", help="the platform owner's email (required for every action except status)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change; change nothing")
    parser.add_argument("--no-sms", action="store_true", help="don't text the owner's verified phone")
    args = parser.parse_args(argv)
    if args.action != "status" and not args.email:
        parser.error(f"{args.action} needs --email (the owner's email, exactly as shown by `status`)")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
