"""Router removal (soft-delete): the operator-facing "Remove router" action.

Sequence matters: active sessions must be kicked while the tunnel is still up
(the RouterOS API needs a live route to the router), so disconnecting existing
customers happens BEFORE the WireGuard peer is torn down. Once the peer is
gone, the router has no path to us at all — that severed tunnel, not any
RADIUS-side eviction, is what actually stops new logins.

Confirmed empirically against this project's own freeradius/ config (isolated
test, not production): FreeRADIUS's SQL-loaded static client list
(`read_clients = yes` in sql.conf) has no TTL and is not re-read on SIGHUP — a
HUP logs "No files changed. Ignoring." and a router that authenticated before
removal keeps authenticating after is_active=false + SIGHUP, identically to
before. Only a full restart re-reads it. Restarting FreeRADIUS is deliberately
NOT done here: it's a shared primary+secondary pair serving every operator on
the platform, so tying a restart to one operator's single-router removal has a
blast radius that doesn't belong in a self-service action.

is_active is never physically deleted: Session, RouterMetric,
RouterProvisionLog, CoAEvent and RouterCredential rows all reference
router_id and must stay valid for the operator's own history. Vouchers and
PaymentTransactions are keyed on site_id, not router_id, and are untouched by
removing a router.

Commits are deliberately incremental, one right after each externally
observable action, rather than one commit at the end. remove_peer() and
disconnect_hotspot_user() are live, non-transactional side effects against
real infrastructure (the kernel wg0 interface, a real RouterOS device) — they
cannot be rolled back. Live verification caught this the hard way: an
unrelated later failure (a NOT NULL constraint on a best-effort audit row)
rolled back one big final commit, leaving the database saying is_active=true,
wg_enabled=true with the peer key still present — while the peer had already
been removed from the real kernel interface. Committing right after each
side effect confines that drift window to a single `await db.commit()`
instead of the whole function, and — critically — means is_active is set
first, before anything that could still fail, so the router is never left
looking untouched in the fleet list after it's already been torn down.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import CoAEvent, Router, Session, Voucher
from src.modules.mikrotik.api_service import MikroTikAPIService, MikroTikOperationError, RouterCredentialsMissingError
from src.modules.mikrotik.types import RouterRemovalSummary
from src.modules.wireguard.service import WireGuardError, WireGuardService

logger = structlog.get_logger(__name__)


class RouterAlreadyRemovedError(Exception):
    """Raised when removal is requested for a router that's already inactive."""


async def remove_router(
    db: AsyncSession,
    router_row: Router,
    isp_operator_id: uuid.UUID,
    *,
    api_service: MikroTikAPIService,
    wg_service: WireGuardService,
) -> RouterRemovalSummary:
    if not router_row.is_active:
        raise RouterAlreadyRemovedError(f"Router {router_row.id} is already removed")

    router_id = str(router_row.id)

    # Mark removed FIRST and commit immediately. This is the single most
    # important state change here — it's what hides the router from the
    # working fleet list and makes a second removal attempt 409 instead of
    # re-running — and it must not be held hostage by anything that happens
    # afterward (see module docstring for why that matters in practice).
    router_row.is_active = False
    router_row.removed_at = datetime.now(timezone.utc)
    await db.commit()

    router_reachable = True
    sessions_disconnected = 0
    sessions_failed = 0

    # 1) Kick everyone currently online, while the tunnel is still up. This is
    #    the only step that can actually force an already-connected customer
    #    off — it issues a direct RouterOS command (/ip/hotspot/active remove),
    #    not RADIUS, so it works regardless of any RADIUS client-cache state.
    try:
        active_users = await api_service.get_active_hotspot_users(router_id)
    except (RouterCredentialsMissingError, MikroTikOperationError) as exc:
        router_reachable = False
        active_users = []
        logger.warning("router_removal_active_users_unreachable", router_id=router_id, error=str(exc))

    for user in active_users:
        session_row = None
        voucher_id = None
        if user.user:
            voucher = (
                await db.execute(
                    select(Voucher).where(Voucher.username == user.user, Voucher.isp_operator_id == isp_operator_id)
                )
            ).scalar_one_or_none()
            voucher_id = str(voucher.id) if voucher else None
            if voucher:
                session_row = (
                    await db.execute(
                        select(Session)
                        .where(
                            Session.router_id == router_row.id,
                            Session.username == user.user,
                            Session.isp_operator_id == isp_operator_id,
                            Session.stopped_at.is_(None),
                        )
                        .order_by(Session.started_at.desc())
                    )
                ).scalars().first()
        try:
            await api_service.disconnect_hotspot_user(router_id, user.id)
            sessions_disconnected += 1
            # coa_events.voucher_id is NOT NULL, so an active user whose
            # RouterOS username doesn't match any known voucher (a stale or
            # manually-added hotspot entry) gets disconnected — that part
            # doesn't depend on a voucher — but no audit row, same as the
            # existing single-user disconnect endpoint refusing outright when
            # it can't resolve a voucher, just without blocking this one.
            if voucher_id is not None:
                db.add(CoAEvent(
                    session_id=session_row.id if session_row else None,
                    voucher_id=voucher_id,
                    router_id=router_row.id,
                    isp_operator_id=isp_operator_id,
                    event_type="disconnect",
                    status="confirmed",
                    attempt_count=1,
                    last_attempted_at=datetime.now(timezone.utc),
                ))
                await db.commit()  # this disconnect already happened for real — record it now, not at the end
            else:
                logger.info(
                    "router_removal_disconnect_without_voucher_no_audit_event",
                    router_id=router_id, active_id=user.id, username=user.user,
                )
        except (RouterCredentialsMissingError, MikroTikOperationError) as exc:
            sessions_failed += 1
            logger.warning("router_removal_disconnect_failed", router_id=router_id, user=user.user, error=str(exc))

    # 2) Tear down the WireGuard tunnel. This — not any RADIUS-side action —
    #    is what stops NEW login attempts: once the peer is gone, the router
    #    has no route to us at all, so its own RADIUS request simply times
    #    out (bounded by the router's own timeout setting, not any
    #    FreeRADIUS client-cache TTL).
    #
    #    NOTE — single-router assumption: this removes one peer inline, in the
    #    same request. That's fine at today's fleet size, but if bulk removal
    #    is ever built, doing this in a loop means N sequential wg-manager
    #    HTTP round-trips (and N Postgres advisory-lock acquisitions inside
    #    allocate/deallocate_tunnel_ip) serialized on one request — reconsider
    #    batching or backgrounding it before wiring that up.
    wireguard_removed = False
    wireguard_message = None
    public_key = router_row.wg_peer_public_key
    if public_key:
        try:
            await wg_service.remove_peer(public_key)
            wireguard_removed = True
        except WireGuardError as exc:
            wireguard_message = str(exc)
            logger.warning("router_removal_wg_remove_failed", router_id=router_id, error=wireguard_message)
    else:
        wireguard_removed = True  # nothing to remove — router was never tunneled

    if wireguard_removed:
        # Only release the IP and clear the peer record once the peer is
        # confirmed gone. On failure, deliberately leave wg_peer_public_key
        # set: check_wireguard_tunnels (jobs/wireguard_status.py) uses exactly
        # that — is_active=false with a peer key still present — to find and
        # retry incomplete removals, and it stops the freed IP from being
        # handed to a new router while a stale peer might still exist on the
        # live interface under the old router's key.
        await wg_service.deallocate_tunnel_ip(db, router_id)
        router_row.wg_enabled = False
        router_row.wg_peer_public_key = None
        router_row.wg_peer_private_key_encrypted = None
        router_row.wg_tunnel_ip = None
        router_row.wg_is_connected = False
        router_row.wg_last_handshake_at = None
        await db.commit()  # the peer is really gone — record that now, independent of the summary write below

    # Durable removal-outcome record — see models.py's Router.removed_at
    # comment and migration 040. This is what lets the router's own page state
    # accurately, from now on, whether already-online customers were actually
    # confirmed disconnected — not just a one-time API response. If nothing
    # else fails, this is also the last commit; if this one somehow does,
    # is_active is still correctly false and these three columns stay NULL —
    # an honest "outcome unknown", not a false "confirmed clean" (the UI
    # treats anything other than removal_router_reachable == true as needing
    # attention, precisely so a NULL here reads as caution, not success).
    router_row.removal_router_reachable = router_reachable
    router_row.removal_sessions_disconnected = sessions_disconnected
    router_row.removal_sessions_failed = sessions_failed
    await db.commit()

    logger.info(
        "router_removed",
        router_id=router_id,
        sessions_disconnected=sessions_disconnected,
        sessions_failed=sessions_failed,
        wireguard_removed=wireguard_removed,
        router_reachable=router_reachable,
    )

    return RouterRemovalSummary(
        router_id=router_id,
        router_reachable=router_reachable,
        sessions_disconnected=sessions_disconnected,
        sessions_failed=sessions_failed,
        wireguard_removed=wireguard_removed,
        wireguard_message=wireguard_message,
    )
