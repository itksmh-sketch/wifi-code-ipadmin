"""Platform-owner editor for the platform's own notification texts.

GET  /platform/notification-templates                       every event, both channels, with previews
GET  /platform/notification-templates/{event}/{channel}     one template
POST /platform/notification-templates/{event}/{channel}/preview   validate + measure without saving
PUT  /platform/notification-templates/{event}/{channel}     save
POST /platform/notification-templates/{event}/{channel}/reset     restore the shipped default

Every write validates before it stores: placeholders must be ones the event
actually supplies, braces must be well formed, a required placeholder (the
approval temp password) must survive the edit, and an SMS must stay inside
MAX_SMS_SEGMENTS as counted by the real segmentation code. A saved template is
therefore always renderable — and the render path falls back to the default
even so, because a row can also be changed outside this endpoint.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import PlatformNotificationTemplate, PlatformOwner
from src.middleware.auth import get_platform_owner_context
from src.modules.notifications import template_catalog as catalog
from src.modules.notifications import template_store
from src.modules.platform.settings_service import get_platform_name

logger = logging.getLogger("platform.notification_templates")

router = APIRouter(prefix="/platform/notification-templates", tags=["platform-notification-templates"])


class TemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str | None = Field(default=None, max_length=catalog.MAX_SUBJECT_CHARS + 1)
    body_text: str = Field(max_length=catalog.MAX_BODY_CHARS + 1)
    body_html: str | None = Field(default=None, max_length=catalog.MAX_BODY_CHARS + 1)


def _known(event: str, channel: str) -> None:
    if (event, channel) not in catalog.DEFAULTS:
        raise HTTPException(status_code=404, detail=f"No such notification template: {event}/{channel}.")


def _describe(
    event: str,
    channel: str,
    current: catalog.TemplateDef,
    *,
    is_default: bool,
    platform_name: str,
    updated_at=None,
) -> dict:
    definition = catalog.EVENTS[event]
    default = catalog.DEFAULTS[(event, channel)]
    return {
        "event": event,
        "channel": channel,
        "label": definition.label,
        "description": definition.description,
        "subject": current.subject,
        "body_text": current.body_text,
        "body_html": current.body_html,
        "is_default": is_default,
        "updated_at": updated_at,
        "default": {
            "subject": default.subject,
            "body_text": default.body_text,
            "body_html": default.body_html,
        },
        "placeholders": [
            {
                "key": name,
                "description": catalog.PLACEHOLDER_HELP[name],
                "required": name in definition.required,
            }
            for name in definition.placeholders
        ],
        "max_sms_segments": catalog.MAX_SMS_SEGMENTS if channel == catalog.SMS else None,
        "preview": catalog.preview(
            event,
            channel,
            subject=current.subject,
            body_text=current.body_text,
            body_html=current.body_html,
            platform_name=platform_name,
        ),
    }


async def _row(db: AsyncSession, event: str, channel: str) -> PlatformNotificationTemplate | None:
    return (
        await db.execute(
            select(PlatformNotificationTemplate).where(
                PlatformNotificationTemplate.event_type == event,
                PlatformNotificationTemplate.channel == channel,
            )
        )
    ).scalar_one_or_none()


def _matches_default(row: PlatformNotificationTemplate, default: catalog.TemplateDef) -> bool:
    return (
        (row.subject or None) == (default.subject or None)
        and row.body_text == default.body_text
        and (row.body_html or None) == (default.body_html or None)
    )


@router.get("")
async def list_templates(
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    # One query for all eighteen, then resolve from it — the per-template loader
    # would issue a SELECT each time round the loop.
    rows = {(r.event_type, r.channel): r for r in (await db.execute(select(PlatformNotificationTemplate))).scalars()}
    platform_name = await get_platform_name()
    items = []
    for event in catalog.EVENTS:
        for channel in catalog.CHANNELS:
            default = catalog.DEFAULTS[(event, channel)]
            row = rows.get((event, channel))
            current = default if row is None or not (row.body_text or "").strip() else catalog.TemplateDef(
                event=event,
                channel=channel,
                subject=row.subject if channel == catalog.EMAIL else None,
                body_text=row.body_text,
                body_html=row.body_html if channel == catalog.EMAIL else None,
            )
            items.append(
                _describe(
                    event,
                    channel,
                    current,
                    is_default=row is None or _matches_default(row, default),
                    platform_name=platform_name,
                    updated_at=row.updated_at if row else None,
                )
            )
    return {"templates": items}


@router.get("/{event}/{channel}")
async def get_template(
    event: str,
    channel: str,
    db: AsyncSession = Depends(get_db),
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    _known(event, channel)
    current = await template_store.load(db, event, channel)
    row = await _row(db, event, channel)
    return _describe(
        event,
        channel,
        current,
        is_default=row is None or _matches_default(row, catalog.DEFAULTS[(event, channel)]),
        platform_name=await get_platform_name(),
        updated_at=row.updated_at if row else None,
    )


@router.post("/{event}/{channel}/preview")
async def preview_template(
    event: str,
    channel: str,
    payload: TemplateUpdate,
    _: PlatformOwner = Depends(get_platform_owner_context),
):
    """Validate and measure without saving — what the editor calls as you type."""
    _known(event, channel)
    platform_name = await get_platform_name()
    error = catalog.validation_error(
        event,
        channel,
        subject=payload.subject,
        body_text=payload.body_text,
        body_html=payload.body_html,
        platform_name=platform_name,
    )
    if error:
        return {"valid": False, "error": error, "preview": None}
    return {
        "valid": True,
        "error": None,
        "preview": catalog.preview(
            event,
            channel,
            subject=payload.subject,
            body_text=payload.body_text,
            body_html=payload.body_html,
            platform_name=platform_name,
        ),
    }


@router.put("/{event}/{channel}")
async def update_template(
    event: str,
    channel: str,
    payload: TemplateUpdate,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    _known(event, channel)
    subject = payload.subject if channel == catalog.EMAIL else None
    body_html = payload.body_html if channel == catalog.EMAIL else None
    error = catalog.validation_error(
        event,
        channel,
        subject=subject,
        body_text=payload.body_text,
        body_html=body_html,
        platform_name=await get_platform_name(),
    )
    if error:
        raise HTTPException(status_code=400, detail=error)

    row = await _row(db, event, channel)
    if row is None:
        row = PlatformNotificationTemplate(event_type=event, channel=channel)
        db.add(row)
    row.subject = subject
    row.body_text = payload.body_text
    row.body_html = body_html
    row.updated_by_platform_owner_id = owner.id
    await db.commit()
    await db.refresh(row)
    logger.info("notification_template_updated event=%s channel=%s owner=%s", event, channel, owner.id)
    return await get_template(event, channel, db=db, _=owner)


@router.post("/{event}/{channel}/reset")
async def reset_template(
    event: str,
    channel: str,
    db: AsyncSession = Depends(get_db),
    owner: PlatformOwner = Depends(get_platform_owner_context),
):
    """Restore the shipped default text for this one template."""
    _known(event, channel)
    default = catalog.DEFAULTS[(event, channel)]
    row = await _row(db, event, channel)
    if row is None:
        row = PlatformNotificationTemplate(event_type=event, channel=channel)
        db.add(row)
    row.subject = default.subject
    row.body_text = default.body_text
    row.body_html = default.body_html
    row.updated_by_platform_owner_id = owner.id
    await db.commit()
    logger.info("notification_template_reset event=%s channel=%s owner=%s", event, channel, owner.id)
    return await get_template(event, channel, db=db, _=owner)
