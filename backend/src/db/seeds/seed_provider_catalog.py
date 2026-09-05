"""Re-runnable provider catalog seed.

Migration 021 seeds the catalog on first run; this script re-syncs it after new
providers are added to `src/modules/platform/provider_catalog.py`, without
needing a new migration.

Idempotent and safe on production: it refreshes display_name, description,
credential_schema, is_integrated, is_platform_provided and sort_order, but never
touches `is_available` or `platform_rate_per_message` — those are the platform
admin's settings and a redeploy must not undo them.

    docker exec hotspot-backend python -m src.db.seeds.seed_provider_catalog
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sqlalchemy.ext.asyncio import create_async_engine

from src.config import get_settings
from src.modules.platform.provider_catalog import async_sync_provider_catalog

settings = get_settings()


async def seed_provider_catalog() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        count = await async_sync_provider_catalog(conn)
    await engine.dispose()
    print(f"✅ Provider catalog synced ({count} entries; is_available and rates left untouched)")


if __name__ == "__main__":
    asyncio.run(seed_provider_catalog())
