from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import CoAEvent, Router, Session, Voucher
from src.radius.coa_sender import send_disconnect_request
from src.utils.encryption import decrypt_secret


async def create_pending_disconnect_event(
    db: AsyncSession,
    *,
    isp_operator_id,
    voucher_id,
    router_id,
    session_row_id=None,
) -> CoAEvent:
    event = CoAEvent(
        isp_operator_id=isp_operator_id,
        session_id=session_row_id,
        voucher_id=voucher_id,
        router_id=router_id,
        event_type="disconnect",
        status="pending",
        attempt_count=0,
    )
    db.add(event)
    await db.flush()
    return event


async def send_disconnect_with_event(
    db: AsyncSession,
    *,
    event: CoAEvent,
    router: Router,
    voucher: Voucher,
    session: Session | None,
) -> dict:
    now = datetime.now(timezone.utc)
    try:
        if session is None or not session.ip_address:
            raise ValueError("missing_active_session_ip_address")
        if not router.ip_address:
            # CoA/Disconnect is sent directly to the NAS at its IP; a tunnel-only
            # router with no direct IP cannot be reached this way.
            raise ValueError("router_has_no_ip_address")

        decrypted_secret = decrypt_secret(router.nas_secret)
        result = send_disconnect_request(
            router_ip=str(router.ip_address),
            router_secret=decrypted_secret,
            attributes={
                "User-Name": session.username,
                "Framed-IP-Address": str(session.ip_address),
            },
        )
        status = "sent" if result.get("status") == "success" else "failed"
        event.status = status
        event.error_message = None if status == "sent" else (result.get("message") or "coa_send_failed")
    except Exception as exc:
        result = {"status": "failed", "message": str(exc)}
        event.status = "failed"
        event.error_message = str(exc)

    event.attempt_count = int(event.attempt_count or 0) + 1
    event.last_attempted_at = now
    await db.flush()
    return result
