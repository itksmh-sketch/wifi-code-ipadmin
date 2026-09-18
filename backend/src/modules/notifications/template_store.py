"""Load a platform notification template, with the shipped default as backstop.

A notification send must never fail because a template row is missing, was
deleted, or holds text an edit broke. Every lookup here degrades: missing row
-> catalog default; malformed stored text -> the catalog default is rendered in
its place (utils.templating.safe_format's fallback), and the failure is logged
rather than raised.

Rows are read per send rather than cached: platform notifications are low
volume (nine events, a handful of operators), and a cached template could not
see an edit made through the portal without a restart.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from src.db.models import PlatformNotificationTemplate
from src.modules.notifications import template_catalog as catalog
from src.modules.notifications.template_catalog import EMAIL, SMS, TemplateDef

logger = logging.getLogger("notifications.templates")


async def load(db, event: str, channel: str) -> TemplateDef:
    """The stored template, or the shipped default when there is no usable row."""
    default = catalog.DEFAULTS.get((event, channel))
    if default is None:
        raise KeyError(f"unknown notification template {event}/{channel}")
    row = (
        await db.execute(
            select(PlatformNotificationTemplate).where(
                PlatformNotificationTemplate.event_type == event,
                PlatformNotificationTemplate.channel == channel,
            )
        )
    ).scalar_one_or_none()
    if row is None or not (row.body_text or "").strip():
        return default
    return TemplateDef(
        event=event,
        channel=channel,
        subject=row.subject if channel == EMAIL else None,
        body_text=row.body_text,
        body_html=row.body_html if channel == EMAIL else None,
    )


async def _load_safely(event: str, channel: str) -> TemplateDef:
    """Open a short-lived session for the lookup; fall back on any failure.

    Own session for the same reason dispatcher._send_sms opens one: the nine
    notify_* functions are called from ten sites across five modules, none of
    which has a session to spare for this leaf.
    """
    default = catalog.DEFAULTS[(event, channel)]
    try:
        from src.db.base import async_session_factory

        async with async_session_factory() as db:
            return await load(db, event, channel)
    except Exception as exc:
        logger.error(
            "notification_template_lookup_failed event=%s channel=%s error=%s — using default", event, channel, exc
        )
        return default


async def render_sms(event: str, values: dict) -> str:
    template = await _load_safely(event, SMS)
    return catalog.render_sms(template.body_text, values, fallback=catalog.DEFAULTS[(event, SMS)].body_text)


async def render_email(event: str, values: dict) -> tuple[str, str, str]:
    template = await _load_safely(event, EMAIL)
    return catalog.render_email(
        subject=template.subject or "",
        body_text=template.body_text,
        body_html=template.body_html or "",
        values=values,
        fallback=catalog.DEFAULTS[(event, EMAIL)],
    )
