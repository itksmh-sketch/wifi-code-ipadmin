from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, or_, cast
from sqlalchemy.dialects.postgresql import INET
from src.db.models import Voucher, Session, Router, Plan
from src.modules.vouchers.engine import transition_voucher_status
from src.radius.coa_events import create_pending_disconnect_event, send_disconnect_with_event
import structlog

logger = structlog.get_logger(__name__)


async def _mark_router_heartbeat(db: AsyncSession, nas_ip: str | None):
    if not nas_ip:
        return
    await db.execute(
        update(Router)
        .where(Router.ip_address == cast(nas_ip, INET))
        .values(last_seen_at=datetime.now(timezone.utc), is_online=True)
    )


async def handle_accounting_start(db: AsyncSession, data: dict):
    """Handle RADIUS Accounting-Start packet."""
    username = data.get("username")
    session_id = data.get("acct_session_id")
    nas_ip = data.get("nas_ip_address")
    mac_address = data.get("calling_station_id")
    ip_address = data.get("framed_ip_address")
    nas_identifier = data.get("nas_identifier")

    if not username or not session_id:
        logger.warning("accounting_start_missing_fields", module=__name__)
        return

    await _mark_router_heartbeat(db, nas_ip)

    # Find router by NAS identifier
    router_result = await db.execute(select(Router).where(Router.nas_identifier == nas_identifier))
    router = router_result.scalar_one_or_none()
    if not router:
        logger.warning("accounting_start_router_not_found", module=__name__, nas_identifier=nas_identifier)
        return

    # Find voucher
    voucher_result = await db.execute(
        select(Voucher).where(
            or_(Voucher.username == username, Voucher.code == username),
            Voucher.isp_operator_id == router.isp_operator_id,
        )
    )
    voucher = voucher_result.scalar_one_or_none()
    if not voucher:
        logger.warning("accounting_start_voucher_not_found", module=__name__, username=username)
        return

    # Transition unused → active on first login
    if voucher.status == "unused":
        try:
            await transition_voucher_status(db, voucher.id, "active", router.isp_operator_id)
            logger.info("voucher_activated", module=__name__, voucher_id=str(voucher.id), code=voucher.code)
        except ValueError as e:
            logger.warning("voucher_activation_failed", module=__name__, voucher_id=str(voucher.id), error=str(e))

    # Create session record
    session = Session(
        isp_operator_id=router.isp_operator_id,
        voucher_id=voucher.id,
        router_id=router.id,
        username=username,
        mac_address=mac_address,
        ip_address=ip_address,
        nas_ip=nas_ip,
        session_id=session_id,
        started_at=datetime.now(timezone.utc),
        upload_bytes=0,
        download_bytes=0,
    )
    db.add(session)
    await db.commit()
    logger.info("session_started", module=__name__, session_id=session_id, username=username, voucher_id=str(voucher.id))


async def handle_accounting_interim_update(db: AsyncSession, data: dict):
    """Handle RADIUS Accounting-Interim-Update packet — update usage."""
    session_id = data.get("acct_session_id")
    nas_ip = data.get("nas_ip_address")
    input_octets = int(data.get("acct_input_octets", 0))
    output_octets = int(data.get("acct_output_octets", 0))

    if not session_id:
        return

    await _mark_router_heartbeat(db, nas_ip)

    # Update session bytes
    await db.execute(
        update(Session)
        .where(Session.session_id == session_id)
        .values(
            upload_bytes=input_octets,
            download_bytes=output_octets,
        )
    )

    # Calculate data used in MB
    total_bytes = input_octets + output_octets
    total_mb = total_bytes / (1024 * 1024)

    # Update voucher data_used_mb
    session_result = await db.execute(select(Session).where(Session.session_id == session_id))
    session = session_result.scalar_one_or_none()
    if session:
        await db.execute(
            update(Voucher)
            .where(Voucher.id == session.voucher_id, Voucher.isp_operator_id == session.isp_operator_id)
            .values(data_used_mb=int(total_mb))
        )

        # Check if data limit exceeded
        voucher_result = await db.execute(select(Voucher).where(Voucher.id == session.voucher_id, Voucher.isp_operator_id == session.isp_operator_id))
        voucher = voucher_result.scalar_one_or_none()
        if voucher and voucher.status == "active":
            plan_result = await db.execute(select(Plan).where(Plan.id == voucher.plan_id, Plan.isp_operator_id == session.isp_operator_id))
            plan = plan_result.scalar_one_or_none()
            if plan and plan.data_limit_mb and int(total_mb) >= plan.data_limit_mb:
                try:
                    await transition_voucher_status(db, voucher.id, "exhausted", session.isp_operator_id)
                    logger.info("voucher_exhausted", module=__name__, voucher_id=str(voucher.id), code=voucher.code)
                    # Send disconnect to router
                    router_result = await db.execute(select(Router).where(Router.id == session.router_id, Router.isp_operator_id == session.isp_operator_id))
                    router = router_result.scalar_one_or_none()
                    if router:
                        event = await create_pending_disconnect_event(
                            db,
                            isp_operator_id=session.isp_operator_id,
                            voucher_id=voucher.id,
                            router_id=router.id,
                            session_row_id=session.id,
                        )
                        await send_disconnect_with_event(
                            db,
                            event=event,
                            router=router,
                            voucher=voucher,
                            session=session,
                        )
                except ValueError as e:
                    logger.warning("voucher_exhaust_failed", module=__name__, voucher_id=str(voucher.id), error=str(e))

    await db.commit()


async def handle_accounting_stop(db: AsyncSession, data: dict):
    """Handle RADIUS Accounting-Stop packet."""
    session_id = data.get("acct_session_id")
    nas_ip = data.get("nas_ip_address")
    input_octets = int(data.get("acct_input_octets", 0))
    output_octets = int(data.get("acct_output_octets", 0))
    terminate_cause = data.get("acct_terminate_cause")

    if not session_id:
        return

    await _mark_router_heartbeat(db, nas_ip)

    # Update and close session
    await db.execute(
        update(Session)
        .where(Session.session_id == session_id)
        .values(
            stopped_at=datetime.now(timezone.utc),
            terminate_cause=terminate_cause,
            upload_bytes=input_octets,
            download_bytes=output_octets,
        )
    )

    # Final data usage update
    total_bytes = input_octets + output_octets
    total_mb = total_bytes / (1024 * 1024)

    session_result = await db.execute(select(Session).where(Session.session_id == session_id))
    session = session_result.scalar_one_or_none()
    if session:
        await db.execute(
            update(Voucher)
            .where(Voucher.id == session.voucher_id, Voucher.isp_operator_id == session.isp_operator_id)
            .values(data_used_mb=int(total_mb))
        )

    await db.commit()
    logger.info("session_stopped", module=__name__, session_id=session_id, terminate_cause=terminate_cause)


async def handle_postauth(db: AsyncSession, data: dict):
    """Log successful authentication attempt."""
    username = data.get("username")
    result = data.get("auth_result")  # "accept" or "reject"
    logger.info("radius_postauth", module=__name__, auth_result=result, username=username)
