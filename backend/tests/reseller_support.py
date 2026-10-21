"""Shared by the reseller consignment suites (migration 060): factories for an
operator with a router, plans, admins and resellers, and RADIUS's own queries
read from freeradius/sql.conf so redemption is exercised exactly as a router
triggers it.

Imported only from inside each suite's ``if FLOW_DB:`` block, so nothing here
binds the database engine unless a throwaway database is configured.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.base import async_session_factory
from src.db.models import AdminUser, ISPOperator, Plan, Reseller, Router, Site, Town, Voucher
from src.modules.auth.tokens import admin_token_response
from src.utils.auth import hash_password
from src.utils.encryption import encrypt_secret
from src.utils.portal_token import create_portal_router_token
from src.utils.reseller_auth import hash_reseller_password

PASSWORD = "Str0ngPassword"
_TUNNEL = iter(range(10, 250))
_NET = iter(range(102, 199))


def now() -> datetime:
    return datetime.now(timezone.utc)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── RADIUS, as sql.conf has it ────────────────────────────────────────────


def _freeradius_dir() -> Path:
    for candidate in (os.getenv("FREERADIUS_DIR"), "/freeradius", Path(__file__).resolve().parents[2] / "freeradius"):
        if candidate and (Path(candidate) / "sql.conf").is_file():
            return Path(candidate)
    pytest.fail("freeradius/sql.conf not found: mount the repo's freeradius/ at /freeradius or set FREERADIUS_DIR")


def _bind_expansions(query: str) -> str:
    """Every '%{Attr-Name}' becomes a text bind parameter :attr_name."""
    query = re.sub(r"'%\{([A-Za-z-]+)\}'", lambda m: f"CAST(:{m.group(1).lower().replace('-', '_')} AS text)", query)
    assert "%{" not in query, f"unhandled expansion: {query}"
    return query


def accounting_start_query() -> str:
    conf = (_freeradius_dir() / "sql.conf").read_text()
    match = re.search(r'\bstart\s*\{.*?query\s*=\s*"(.*?)"\s*\n', conf, re.S)
    assert match, "accounting start query not found in sql.conf"
    return _bind_expansions(match.group(1).replace("\\\n", " "))


def authorize_check_query() -> str:
    conf = (_freeradius_dir() / "sql.conf").read_text()
    match = re.search(r'^\s*authorize_check_query\s*=\s*"(.*)"\s*$', conf, re.M)
    assert match, "authorize_check_query not found in sql.conf"
    return _bind_expansions(match.group(1))


async def accounting_start(voucher: "Voucher", router: "Router", session_id: str | None = None) -> str:
    """What the router sends when a customer comes online with this voucher.
    Returns the Acct-Session-Id, for a retry."""
    session_id = session_id or f"S-{uuid.uuid4().hex[:12]}"
    async with async_session_factory() as db:
        await db.execute(text(accounting_start_query()), {
            "sql_user_name": voucher.username, "calling_station_id": "AA:BB:CC:00:11:22",
            "framed_ip_address": f"10.5.50.{next(_NET)}", "nas_ip_address": str(router.wg_tunnel_ip),
            "acct_session_id": session_id,
        })
        await db.commit()
    return session_id


async def radius_accepts(voucher: "Voucher", router: "Router") -> bool:
    async with async_session_factory() as db:
        rows = (await db.execute(text(authorize_check_query()),
                                 {"sql_user_name": voucher.username, "nas_ip_address": str(router.wg_tunnel_ip)})).all()
    return bool(rows)


# ── rows ──────────────────────────────────────────────────────────────────


async def make_operator(**values) -> "ISPOperator":
    slug = f"rv-{uuid.uuid4().hex[:10]}"
    async with async_session_factory() as db:
        operator = ISPOperator(name=f"Rv {slug}", slug=slug, contact_email=f"{slug}@throwaway.test",
                               monthly_fee_ghs=Decimal("50.00"), status="approved", billing_status="active", **values)
        db.add(operator)
        await db.commit()
        return operator


async def make_router(operator) -> "Router":
    async with async_session_factory() as db:
        town = Town(isp_operator_id=operator.id, name=f"T {uuid.uuid4().hex[:6]}", region="Volta")
        db.add(town)
        await db.flush()
        site = Site(isp_operator_id=operator.id, town_id=town.id, name="S", address="1 Test Road")
        db.add(site)
        await db.flush()
        router = Router(isp_operator_id=operator.id, site_id=site.id, name=f"R-{uuid.uuid4().hex[:6]}",
                        nas_identifier=f"nas-{uuid.uuid4().hex[:6]}", nas_secret=encrypt_secret("rt-secret"),
                        wg_tunnel_ip=f"10.102.{next(_TUNNEL)}.2", is_active=True, is_online=True)
        db.add(router)
        await db.commit()
        return router


async def portal_login(client, code: str, router):
    """POST /portal/authenticate as the router's login page sends it."""
    return await client.post("http://testserver/portal/authenticate", data={
        "code": code, "rt": create_portal_router_token(str(router.id)), "gateway": "10.5.50.1",
    })


async def make_plan(operator, *, price="10.00", minutes=60, is_active=True) -> "Plan":
    async with async_session_factory() as db:
        plan = Plan(isp_operator_id=operator.id, name=f"P {uuid.uuid4().hex[:6]}", type="time",
                    duration_minutes=minutes, download_speed_kbps=2048, upload_speed_kbps=1024,
                    price_ghs=Decimal(price), is_active=is_active)
        db.add(plan)
        await db.commit()
        return plan


async def make_admin(operator, *, role="superadmin", pin=True) -> tuple["AdminUser", dict]:
    """An admin past onboarding, PIN-elevated for the next hour when ``pin``,
    and their bearer headers."""
    async with async_session_factory() as db:
        admin = AdminUser(
            isp_operator_id=operator.id, email=f"adm-{uuid.uuid4().hex[:10]}@throwaway.test",
            password_hash=hash_password(PASSWORD), role=role, is_active=True, phone="233244000777",
            phone_verified=True, must_complete_onboarding=False, must_change_password=False,
            security_question="birth_town", security_answer_hash=hash_password("accra"),
            pin_hash=hash_password("482913") if pin else None,
            pin_verified_until=now() + timedelta(hours=1) if pin else None,
        )
        db.add(admin)
        await db.commit()
    return admin, bearer(admin_token_response(admin).access_token)


async def make_reseller(operator, **values) -> "Reseller":
    async with async_session_factory() as db:
        state = dict(isp_operator_id=operator.id, name="Res", email=f"res-{uuid.uuid4().hex[:10]}@throwaway.test",
                     password_hash=hash_reseller_password(PASSWORD), role="reseller", is_active=True)
        reseller = Reseller(**{**state, **values})
        db.add(reseller)
        await db.commit()
        return reseller


async def reseller_headers(client, reseller) -> dict:
    res = await client.post("/reseller/auth/login", json={"email": reseller.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return bearer(res.json()["access_token"])


async def voucher_by_code(code: str) -> "Voucher":
    from sqlalchemy import select

    async with async_session_factory() as db:
        return (await db.execute(select(Voucher).where(Voucher.code == code))).scalar_one()


async def fresh(model, row_id):
    async with async_session_factory() as db:
        return await db.get(model, row_id)


async def redemptions(voucher_id=None, reseller_id=None) -> list:
    from src.db.models import ResellerRedemption
    from sqlalchemy import select

    async with async_session_factory() as db:
        q = select(ResellerRedemption)
        if voucher_id is not None:
            q = q.where(ResellerRedemption.voucher_id == voucher_id)
        if reseller_id is not None:
            q = q.where(ResellerRedemption.reseller_id == reseller_id)
        return (await db.execute(q.order_by(ResellerRedemption.redeemed_at))).scalars().all()


async def audit_rows(action: str, target_id) -> list:
    from src.db.models import AuditLog
    from sqlalchemy import select

    async with async_session_factory() as db:
        q = select(AuditLog).where(AuditLog.action == action, AuditLog.target_id == str(target_id))
        return (await db.execute(q.order_by(AuditLog.created_at))).scalars().all()
