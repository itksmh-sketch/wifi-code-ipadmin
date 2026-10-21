"""
Seed script: Creates test data for Phase 1.
- 1 town
- 2 sites
- 2 routers
- 3 plans (1 time, 1 data, 1 hybrid)
- 10 test vouchers
- 1 superadmin user

Safe to run multiple times (idempotent).

Platform owner security setup (non-production opt-in)
------------------------------------------------------
Every platform-owner route is gated until the owner completes security setup,
and setup needs a working SMS account to verify a phone. A dev/CI install with
no SMS would leave the seeded owner stuck at the setup wizard. For those
environments only, the seed can create the owner with setup already complete:

    SEED_OWNER_SETUP_COMPLETE=true
    SEED_OWNER_CHARACTER_CODE=<12 characters from ABCDEFGHJKMNPQRSTUVWXYZ23456789>

e.g. docker exec -e SEED_OWNER_SETUP_COMPLETE=true -e SEED_OWNER_CHARACTER_CODE=ABCDEFGHJK23 \
       -e PLATFORM_OWNER_EMAIL=owner@ci.test hotspot-backend python -m src.db.seeds.seed

Guard rails, all checked before anything is written:
  * off unless SEED_OWNER_SETUP_COMPLETE is true/1/yes; any other non-false
    value is refused (a typo must not silently mean "off" or "on");
  * the code must be a valid 12-character code;
  * the owner email must be a reserved test address (.test, .example,
    .invalid, .localhost, .local, example.com/.org/.net). The production
    owner's address is not, so the opt-in refuses there;
  * INSERT-ONLY: it applies only when this run creates the owner. An owner
    that already exists is never modified, whatever the variables say.
There is deliberately no runtime switch that skips the setup gate.

Live-database guard
-------------------
The seed refuses (exit 2, nothing written) when the database looks live: any
operator other than the seed's own "tenant-zero", or any router with a
wg_tunnel_ip. It applies however the seed is started (RUN_SEED, docker exec,
python -m) and has no override. Seeding is for empty dev/CI databases only.
"""
import asyncio
import os
import secrets
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
from src.db.models import PlatformOwnerSecurityEvent
from src.modules.platform.owner_setup import is_valid_code, looks_like_test_email, mark_setup_complete
from src.utils.reseller_auth import hash_reseller_password
from src.utils.encryption import encrypt_secret
from src.db.models import Reseller

settings = get_settings()

# Shipped in .env.example and as docker-compose fallbacks, so public: the seed
# treats a password still set to one of these as unset.
SHIPPED_DEFAULT_PASSWORDS = {"admin123", "change-me-strong-password"}


def random_password() -> str:
    return secrets.token_urlsafe(12)


def seed_password(configured: str | None) -> tuple[str, bool]:
    """The configured password, or a random one when it is unset or a shipped
    default. Returns (password, generated); a generated one is printed once by
    the caller and stored nowhere else."""
    if configured and configured not in SHIPPED_DEFAULT_PASSWORDS:
        return configured, False
    return random_password(), True


SEED_SETUP_FLAG = "SEED_OWNER_SETUP_COMPLETE"
SEED_CODE_VAR = "SEED_OWNER_CHARACTER_CODE"


class SeedRefused(Exception):
    """A seed opt-in is misconfigured. Raised before anything is written."""


def owner_setup_opt_in(env=None, email: str | None = None) -> str | None:
    """The character code to seed the owner with, or None if the opt-in is off.
    Raises SeedRefused for anything half-configured or unsafe."""
    env = os.environ if env is None else env
    email = settings.platform_owner_email if email is None else email
    raw = env.get(SEED_SETUP_FLAG, "")
    flag = raw.strip().lower()
    code = env.get(SEED_CODE_VAR, "").strip().upper()
    if flag in ("", "false", "0", "no"):
        if code:
            print(f"  {SEED_CODE_VAR} is set but {SEED_SETUP_FLAG} is not true: ignored.")
        return None
    if flag not in ("true", "1", "yes"):
        raise SeedRefused(f"{SEED_SETUP_FLAG}={raw!r} is not true/false.")
    if not is_valid_code(code):
        raise SeedRefused(
            f"{SEED_SETUP_FLAG} is on but {SEED_CODE_VAR} is missing or invalid: it must be "
            "12 characters from ABCDEFGHJKMNPQRSTUVWXYZ23456789."
        )
    if not looks_like_test_email(email):
        raise SeedRefused(
            f"{SEED_SETUP_FLAG} is on but the owner email {email!r} is not a reserved test address "
            "(.test, .example, .invalid, .localhost, .local, example.com/.org/.net). "
            "This opt-in is for dev/CI databases only."
        )
    return code


SEED_OPERATOR_SLUG = "tenant-zero"


async def refuse_if_live(db: AsyncSession) -> None:
    """Raise SeedRefused if this database holds anything the seed did not make."""
    others = (await db.execute(
        select(func.count()).select_from(ISPOperator).where(ISPOperator.slug != SEED_OPERATOR_SLUG)
    )).scalar_one()
    tunnelled = (await db.execute(
        select(func.count()).select_from(Router).where(Router.wg_tunnel_ip.is_not(None))
    )).scalar_one()
    if others or tunnelled:
        raise SeedRefused(
            f"this database looks live: {others} operator(s) other than {SEED_OPERATOR_SLUG!r}, "
            f"{tunnelled} router(s) with a wg_tunnel_ip. The seed is for empty dev/CI databases only."
        )


BATCH_ID = "SEED-BATCH-001"


async def seed():
    # Validated before the engine exists: a refused opt-in writes nothing.
    owner_setup_code = owner_setup_opt_in()
    engine = create_async_engine(settings.database_url)
    async_session = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session() as db:
        await refuse_if_live(db)
        print("🌱 Seeding database...")
        owner = (
            await db.execute(select(PlatformOwner).where(PlatformOwner.email == settings.platform_owner_email))
        ).scalar_one_or_none()
        if owner is None:
            owner_password, owner_generated = seed_password(settings.platform_owner_password)
            owner = PlatformOwner(
                email=settings.platform_owner_email,
                password_hash=hash_password(owner_password),
                name="Platform Owner",
                is_active=True,
            )
            db.add(owner)
            if owner_setup_code:
                await db.flush()  # the code's digests are bound to the owner id
                mark_setup_complete(owner, code=owner_setup_code)
                db.add(PlatformOwnerSecurityEvent(
                    platform_owner_id=owner.id,
                    event_type="seeded_setup_complete",
                    detail={"source": "seed.py", "opt_in": SEED_SETUP_FLAG},
                ))
            await db.commit()
            print(f"  Platform owner created: {owner.email}")
            if owner_generated:
                print(f"  Generated password (shown once): {owner_password}")
            if owner_setup_code:
                print(f"  Security setup marked complete ({SEED_SETUP_FLAG}); sign in with the password "
                      f"and characters from {SEED_CODE_VAR}.")
        else:
            print(f"  Platform owner already exists: {owner.email}")
            if owner_setup_code:
                print(f"  {SEED_SETUP_FLAG} ignored: the seed never modifies an existing owner.")

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
            admin_password, admin_generated = seed_password(settings.admin_password)
            admin = AdminUser(
                isp_operator_id=tenant_zero_id,
                email=settings.admin_email,
                password_hash=hash_password(admin_password),
                role="superadmin",
                is_active=True,
            )
            db.add(admin)
            await db.commit()
            if admin_generated:
                print(f"  ✅ Superadmin: {admin.email} / {admin_password} (generated, shown once)")
            else:
                print(f"  ✅ Superadmin: {admin.email} (password from ADMIN_PASSWORD)")
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

        # --- Phase 3: Resellers (codes are allocated to them from the dashboard) ---
        async def ensure_reseller(*, name: str, email: str, phone: str, role: str, town_id, site_id):
            r3 = await db.execute(select(Reseller).where(Reseller.email == email))
            existing_reseller = r3.scalar_one_or_none()
            if existing_reseller:
                print(f"  ⏭ Reseller already exists: {email}")
                return existing_reseller
            password = random_password()
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
            print(f"  ✅ Reseller: {email} ({role}) / {password} (generated, shown once)")
            return rr

        reseller1 = await ensure_reseller(
            name="Reseller One",
            email="reseller1@example.com",
            phone="233244000001",
            role="reseller",
            town_id=town.id,
            site_id=site1.id,
        )
        await ensure_reseller(
            name="Town Agent One",
            email="agent1@example.com",
            phone="233244000002",
            role="town_agent",
            town_id=town.id,
            site_id=None,
        )
        # 10% on every code reseller1 sells; the town agent earns the operator
        # default (0% unless set).
        if reseller1.commission_type is None:
            reseller1.commission_type = "percent"
            reseller1.commission_value = 10
            await db.commit()
            print("  ✅ Commission set: reseller1 10%")
        else:
            print("  ⏭ Commission already set: reseller1")

        print("\n🎉 Seeding complete!")


if __name__ == "__main__":
    try:
        asyncio.run(seed())
    except SeedRefused as exc:
        print(f"SEED REFUSED, nothing was written: {exc}", file=sys.stderr)
        sys.exit(2)
