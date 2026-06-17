"""
Seed script: Creates test data for Phase 1.
- 1 town
- 2 sites
- 2 routers
- 3 plans (1 time, 1 data, 1 hybrid)
- 10 test vouchers
- 1 superadmin user

Safe to run multiple times (idempotent).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.db.models import AdminUser, ConfigTemplate, ISPOperator, Plan, PlatformOwner, Router, RouterCredential, Site, Town, Voucher
from src.modules.vouchers.engine import (
    generate_voucher_code,
    generate_voucher_password,
    generate_voucher_username,
)
from src.utils.auth import hash_password
from src.utils.reseller_auth import hash_reseller_password
from src.utils.encryption import encrypt_secret
from src.db.models import CommissionRule, Reseller, ResellerVoucherAllocation, ResellerWallet, ResellerWalletTransaction
from src.modules.resellers.wallet_service import WalletService

settings = get_settings()

BATCH_ID = "SEED-BATCH-001"
RESELLER_SEED_BATCH = "SEED-RESELLER-ALLOC"


async def seed():
    engine = create_async_engine(settings.database_url)
    async_session = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session() as db:
        print("🌱 Seeding database...")
        wallet_service = WalletService()
        owner = (
            await db.execute(select(PlatformOwner).where(PlatformOwner.email == settings.platform_owner_email))
        ).scalar_one_or_none()
        if owner is None:
            owner = PlatformOwner(
                email=settings.platform_owner_email,
                password_hash=hash_password(settings.platform_owner_password),
                name="Platform Owner",
                is_active=True,
            )
            db.add(owner)
            await db.commit()
            print(f"  Platform owner created: {owner.email}")
        else:
            print(f"  Platform owner already exists: {owner.email}")

        tenant_zero = (
            await db.execute(select(ISPOperator).where(ISPOperator.slug == "tenant-zero"))
        ).scalar_one_or_none()
        if tenant_zero is None:
            tenant_zero = ISPOperator(
                name="Tenant Zero",
                slug="tenant-zero",
                contact_email=settings.platform_owner_email,
                status="approved",
                billing_status="active",
            )
            db.add(tenant_zero)
            await db.commit()
        tenant_zero_id = tenant_zero.id

        # --- Superadmin ---
        r = await db.execute(select(AdminUser).where(AdminUser.email == settings.admin_email))
        admin = r.scalar_one_or_none()
        if admin is None:
            admin = AdminUser(
                isp_operator_id=tenant_zero_id,
                email=settings.admin_email,
                password_hash=hash_password(settings.admin_password),
                role="superadmin",
                is_active=True,
            )
            db.add(admin)
            await db.commit()
            print(f"  ✅ Superadmin: {admin.email} / {settings.admin_password}")
        else:
            print(f"  ⏭ Superadmin already exists: {admin.email}")

        # --- Town ---
        r = await db.execute(
            select(Town).where(Town.name == "Accra", Town.region == "Greater Accra")
        )
        town = r.scalar_one_or_none()
        if town is None:
            town = Town(isp_operator_id=tenant_zero_id, name="Accra", region="Greater Accra")
            db.add(town)
            await db.commit()
            print(f"  ✅ Town: {town.name} ({town.region})")
        else:
            print(f"  ⏭ Town already exists: {town.name}")

        # --- Sites ---
        r = await db.execute(
            select(Site).where(Site.town_id == town.id, Site.name == "Osu Oxford Street")
        )
        site1 = r.scalar_one_or_none()
        if site1 is None:
            site1 = Site(isp_operator_id=tenant_zero_id, town_id=town.id, name="Osu Oxford Street", address="Osu, Accra")
            db.add(site1)
            await db.commit()
            print(f"  ✅ Site: {site1.name}")
        else:
            print(f"  ⏭ Site already exists: {site1.name}")

        r = await db.execute(select(Site).where(Site.town_id == town.id, Site.name == "East Legon"))
        site2 = r.scalar_one_or_none()
        if site2 is None:
            site2 = Site(isp_operator_id=tenant_zero_id, town_id=town.id, name="East Legon", address="East Legon, Accra")
            db.add(site2)
            await db.commit()
            print(f"  ✅ Site: {site2.name}")
        else:
            print(f"  ⏭ Site already exists: {site2.name}")

        # --- Routers ---
        r = await db.execute(select(Router).where(Router.nas_identifier == "router-osu-01"))
        router1 = r.scalar_one_or_none()
        if router1 is None:
            router1 = Router(
                isp_operator_id=tenant_zero_id,
                site_id=site1.id,
                name="MikroTik-Osu",
                ip_address="192.168.1.1",
                nas_identifier="router-osu-01",
                nas_secret=encrypt_secret("testing123"),
                nas_secret_plain="testing123",  # FreeRADIUS client_query reads this
                is_active=True,
            )
            db.add(router1)
            await db.commit()
            print(f"  ✅ Router: {router1.name} ({router1.ip_address})")
        else:
            router1.ip_address = "192.168.1.1"
            await db.commit()
            print(f"  ⏭ Router already exists: {router1.name}")

        r = await db.execute(select(Router).where(Router.nas_identifier == "router-eastlegon-01"))
        router2 = r.scalar_one_or_none()
        if router2 is None:
            router2 = Router(
                isp_operator_id=tenant_zero_id,
                site_id=site2.id,
                name="MikroTik-EastLegon",
                ip_address="192.168.1.2",
                nas_identifier="router-eastlegon-01",
                nas_secret=encrypt_secret("testing123"),
                nas_secret_plain="testing123",  # FreeRADIUS client_query reads this
                is_active=True,
            )
            db.add(router2)
            await db.commit()
            print(f"  ✅ Router: {router2.name} ({router2.ip_address})")
        else:
            router2.ip_address = "192.168.1.2"
            await db.commit()
            print(f"  ⏭ Router already exists: {router2.name}")

        async def ensure_router_credentials(router: Router, api_username: str, api_password: str, api_port: int, use_ssl: bool):
            existing_credentials = (
                await db.execute(select(RouterCredential).where(RouterCredential.router_id == router.id))
            ).scalar_one_or_none()
            if existing_credentials is None:
                existing_credentials = RouterCredential(
                    router_id=router.id,
                    api_username=api_username,
                    api_password_encrypted=encrypt_secret(api_password),
                    api_port=api_port,
                    use_ssl=use_ssl,
                    connection_status="unknown",
                )
                db.add(existing_credentials)
                await db.commit()
                print(f"  ✅ Router credentials seeded for {router.name}")
            else:
                existing_credentials.api_username = api_username
                existing_credentials.api_password_encrypted = encrypt_secret(api_password)
                existing_credentials.api_port = api_port
                existing_credentials.use_ssl = use_ssl
                await db.commit()
                print(f"  ⏭ Router credentials already exist for {router.name}")

        await ensure_router_credentials(router1, "admin", "admin", 8728, False)
        await ensure_router_credentials(router2, "admin", "admin", 8728, False)

        standard_template_data = {
            "version": 1,
            "commands": [
                {
                    "path": "/ip/hotspot/profile/set",
                    "params": {
                        "name": "hsprof1",
                        "use-radius": "yes",
                        "nas-port-type": "wireless-802.11",
                    },
                },
                {
                    "path": "/radius/add",
                    "params": {
                        "service": "hotspot",
                        "address": "{RADIUS_HOST}",
                        "secret": "{NAS_SECRET}",
                        "authentication-port": 1812,
                        "accounting-port": 1813,
                    },
                },
            ],
        }
        existing_template = (
            await db.execute(select(ConfigTemplate).where(ConfigTemplate.name == "Standard hotspot setup"))
        ).scalar_one_or_none()
        if existing_template is None:
            existing_template = ConfigTemplate(
                isp_operator_id=None,
                name="Standard hotspot setup",
                description="Default baseline for MikroTik hotspot + RADIUS",
                template_data=standard_template_data,
                is_default=True,
            )
            db.add(existing_template)
            await db.commit()
            print("  ✅ Default config template seeded")
        else:
            existing_template.description = "Default baseline for MikroTik hotspot + RADIUS"
            existing_template.template_data = standard_template_data
            existing_template.is_default = True
            await db.commit()
            print("  ⏭ Default config template already exists")

        # --- Plans (match by name from this seed) ---
        async def ensure_plan(**kwargs) -> Plan:
            name = kwargs["name"]
            r2 = await db.execute(select(Plan).where(Plan.name == name))
            existing = r2.scalar_one_or_none()
            if existing is not None:
                print(f"  ⏭ Plan already exists: {name}")
                return existing
            p = Plan(**kwargs)
            p.isp_operator_id = tenant_zero_id
            db.add(p)
            await db.commit()
            print(f"  ✅ Plan: {p.name}")
            return p

        plan_time = await ensure_plan(
            name="1 Hour Pass",
            type="time",
            duration_minutes=60,
            data_limit_mb=None,
            download_speed_kbps=2048,
            upload_speed_kbps=1024,
            price_ghs=2.00,
            is_active=True,
        )
        plan_data = await ensure_plan(
            name="500 MB Pass",
            type="data",
            duration_minutes=None,
            data_limit_mb=500,
            download_speed_kbps=1024,
            upload_speed_kbps=512,
            price_ghs=5.00,
            is_active=True,
        )
        plan_hybrid = await ensure_plan(
            name="Daily Unlimited",
            type="hybrid",
            duration_minutes=1440,
            data_limit_mb=None,
            download_speed_kbps=4096,
            upload_speed_kbps=2048,
            price_ghs=10.00,
            is_active=True,
        )
        plan_time_id = plan_time.id
        plan_time_duration = plan_time.duration_minutes

        # --- 10 Test Vouchers ---
        cnt = (
            await db.execute(
                select(func.count()).select_from(Voucher).where(Voucher.batch_id == BATCH_ID)
            )
        ).scalar_one()
        if cnt == 0:
            vouchers = []
            for i in range(10):
                plan = [plan_time, plan_data, plan_hybrid][i % 3]
                code = generate_voucher_code()
                username = generate_voucher_username()
                password = generate_voucher_password()

                expires_at = None
                if plan.duration_minutes:
                    expires_at = datetime.now(timezone.utc) + timedelta(minutes=plan.duration_minutes)

                voucher = Voucher(
                    isp_operator_id=tenant_zero_id,
                    plan_id=plan.id,
                    site_id=site1.id if i < 5 else site2.id,
                    code=code,
                    username=username,
                    password=password,
                    status="unused",
                    device_policy="single",
                    max_devices=1,
                    expires_at=expires_at,
                    batch_id=BATCH_ID,
                )
                vouchers.append(voucher)

            db.add_all(vouchers)
            await db.commit()
            print(f"  ✅ 10 vouchers generated (batch: {BATCH_ID})")
            print("\n📋 Test Vouchers:")
            for v in vouchers:
                print(f"   Code: {v.code} | Username: {v.username} | Password: {v.password}")
        else:
            print(f"  ⏭ Vouchers for batch {BATCH_ID} already exist ({cnt} rows)")
            r = await db.execute(select(Voucher).where(Voucher.batch_id == BATCH_ID))
            existing = list(r.scalars().all())
            print("\n📋 Test Vouchers:")
            for v in existing:
                print(f"   Code: {v.code} | Username: {v.username} | Password: {v.password}")

        # --- Phase 3: Resellers + wallets + commission + allocations ---
        async def ensure_reseller(*, name: str, email: str, phone: str, password: str, role: str, town_id, site_id):
            r3 = await db.execute(select(Reseller).where(Reseller.email == email))
            existing_reseller = r3.scalar_one_or_none()
            if existing_reseller:
                print(f"  ⏭ Reseller already exists: {email}")
                return existing_reseller
            rr = Reseller(
                isp_operator_id=tenant_zero_id,
                name=name,
                email=email,
                phone=phone,
                password_hash=hash_reseller_password(password),
                role=role,
                town_id=town_id,
                site_id=site_id,
                is_active=True,
            )
            db.add(rr)
            await db.commit()
            print(f"  ✅ Reseller: {email} ({role})")
            return rr

        reseller1 = await ensure_reseller(
            name="Reseller One",
            email="reseller1@example.com",
            phone="233244000001",
            password="reseller123",
            role="reseller",
            town_id=town.id,
            site_id=site1.id,
        )
        town_agent = await ensure_reseller(
            name="Town Agent One",
            email="agent1@example.com",
            phone="233244000002",
            password="agent123",
            role="town_agent",
            town_id=town.id,
            site_id=None,
        )
        # Store primitives early; rollback/commit can expire ORM instances.
        reseller1_id = reseller1.id
        reseller1_email = reseller1.email
        reseller1_site_id = reseller1.site_id
        town_agent_id = town_agent.id
        town_agent_email = town_agent.email
        town_agent_site_id = town_agent.site_id

        async def ensure_wallet(reseller_id):
            r4 = await db.execute(select(ResellerWallet).where(ResellerWallet.reseller_id == reseller_id))
            w = r4.scalar_one_or_none()
            if w:
                return w
            w = ResellerWallet(reseller_id=reseller_id)
            db.add(w)
            await db.commit()
            print(f"  ✅ Wallet created for reseller {reseller_id}")
            return w

        await ensure_wallet(reseller1_id)
        await ensure_wallet(town_agent_id)

        async def ensure_wallet_funded(reseller_id):
            w = (await db.execute(select(ResellerWallet).where(ResellerWallet.reseller_id == reseller_id))).scalar_one()
            if Decimal(str(w.balance_ghs)) >= Decimal("500.00"):
                print(f"  ⏭ Wallet already funded: reseller {reseller_id} balance={w.balance_ghs}")
                return
            reference = f"seed-topup-{reseller_id}"
            existing_topup = (
                await db.execute(select(ResellerWalletTransaction).where(ResellerWalletTransaction.reference == reference))
            ).scalar_one_or_none()
            if existing_topup is not None:
                print(f"  ⏭ Wallet seed topup already recorded: reseller {reseller_id}")
                return
            # SQLAlchemy sessions auto-begin on SELECT; rollback before starting an explicit transaction ctx.
            await db.rollback()
            async with db.begin():
                await wallet_service.topup(
                    db,
                    reseller_id=reseller_id,
                    amount_ghs=Decimal("500.00"),
                    description="Seed funding",
                    triggered_by="system",
                    reference=reference,
                )
            print(f"  ✅ Wallet funded: reseller {reseller_id} +500.00")

        from decimal import Decimal  # local import for seed script

        await ensure_wallet_funded(reseller1_id)
        await ensure_wallet_funded(town_agent_id)

        # Commission rule: 10% on all plans for reseller1
        r5 = await db.execute(
            select(CommissionRule).where(
                CommissionRule.reseller_id == reseller1_id,
                CommissionRule.plan_id.is_(None),
                CommissionRule.type == "percentage",
                CommissionRule.value == 10,
            )
        )
        if r5.scalar_one_or_none() is None:
            rule = CommissionRule(
                isp_operator_id=tenant_zero_id,
                reseller_id=reseller1_id,
                plan_id=None,
                type="percentage",
                value=Decimal("10.0000"),
                is_active=True,
            )
            db.add(rule)
            await db.commit()
            print("  ✅ Commission rule created: reseller1 10% all plans")
        else:
            print("  ⏭ Commission rule already exists: reseller1 10% all plans")

        # Allocate 5 vouchers to each reseller (purchase workflow in one atomic transaction per reseller)
        async def ensure_allocations(*, reseller_id, reseller_email: str, reseller_site_id, plan_id, plan_duration_minutes, qty: int):
            existing_cnt = (
                await db.execute(
                    select(func.count())
                    .select_from(ResellerVoucherAllocation)
                    .join(Voucher, Voucher.id == ResellerVoucherAllocation.voucher_id)
                    .where(ResellerVoucherAllocation.reseller_id == reseller_id, Voucher.batch_id == RESELLER_SEED_BATCH)
                )
            ).scalar_one()
            if existing_cnt >= qty:
                print(f"  ⏭ Allocations already exist for {reseller_email}: {existing_cnt}")
                return

            to_create = qty - int(existing_cnt)
            await db.rollback()
            async with db.begin():
                for _ in range(to_create):
                    v = Voucher(
                        isp_operator_id=tenant_zero_id,
                        plan_id=plan_id,
                        site_id=reseller_site_id,
                        code=generate_voucher_code(),
                        username=generate_voucher_username(),
                        password=generate_voucher_password(),
                        status="unused",
                        device_policy="single",
                        max_devices=1,
                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=plan_duration_minutes) if plan_duration_minutes else None,
                        batch_id=RESELLER_SEED_BATCH,
                    )
                    db.add(v)
                    await db.flush()
                    await wallet_service.purchase(db, reseller_id=reseller_id, voucher_id=v.id, plan_id=plan_id, triggered_by="system", description="Seed voucher allocation")
            print(f"  ✅ Allocated {to_create} vouchers to {reseller_email}")

        await ensure_allocations(
            reseller_id=reseller1_id,
            reseller_email=reseller1_email,
            reseller_site_id=reseller1_site_id,
            plan_id=plan_time_id,
            plan_duration_minutes=plan_time_duration,
            qty=5,
        )
        await ensure_allocations(
            reseller_id=town_agent_id,
            reseller_email=town_agent_email,
            reseller_site_id=town_agent_site_id,
            plan_id=plan_time_id,
            plan_duration_minutes=plan_time_duration,
            qty=5,
        )

        print("\n🎉 Seeding complete!")


if __name__ == "__main__":
    asyncio.run(seed())
