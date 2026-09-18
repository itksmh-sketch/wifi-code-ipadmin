"""Operator-facing editor for the purchase-confirmation SMS.

GET  /sms-template/voucher          current text (or the default), placeholders, preview
POST /sms-template/voucher/preview  validate + measure without saving
PUT  /sms-template/voucher          save, or reset to the default with {"template": null}
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ISPOperator
from src.middleware.auth import TenantContext, get_admin_tenant_context, require_active_operator
from src.modules.sms import templates
from src.modules.vouchers.engine import DEFAULT_CODE_LENGTH, MAX_CODE_LENGTH

logger = logging.getLogger("sms.template_routes")

router = APIRouter(prefix="/sms-template", tags=["sms-template"])

# Said out loud in the editor rather than silently assumed: validation measures
# the DEFAULT code length, so a plan generated with a longer code can still push
# a real send over one segment.
CODE_LENGTH_CAVEAT = (
    f"Checked against the default {DEFAULT_CODE_LENGTH}-character voucher code "
    f"({len(templates.SAMPLE_CODE)} characters with dashes). A batch generated with a longer "
    f"code length (up to {MAX_CODE_LENGTH}) can still exceed one SMS."
)


class VoucherTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # null resets to the platform default.
    template: str | None = Field(default=None, max_length=templates.MAX_TEMPLATE_CHARS + 1)


class VoucherTemplatePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(max_length=templates.MAX_TEMPLATE_CHARS + 1)


def _payload(operator: ISPOperator) -> dict:
    effective = operator.voucher_sms_template or templates.DEFAULT_VOUCHER_SMS_TEMPLATE
    measured = templates.preview(effective)
    return {
        "template": operator.voucher_sms_template,
        "effective_template": effective,
        "is_default": operator.voucher_sms_template is None,
        "default_template": templates.DEFAULT_VOUCHER_SMS_TEMPLATE,
        "placeholders": [{"key": key, "description": description} for key, description in templates.PLACEHOLDERS.items()],
        "preview": {
            "text": measured.text,
            "encoding": measured.encoding,
            "segment_count": measured.segment_count,
            "character_count": measured.character_count,
        },
        "code_length_caveat": CODE_LENGTH_CAVEAT,
    }


async def _operator(db: AsyncSession, tenant: TenantContext) -> ISPOperator:
    operator = (
        await db.execute(select(ISPOperator).where(ISPOperator.id == tenant.isp_operator_id))
    ).scalar_one_or_none()
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")
    return operator


@router.get("/voucher")
async def get_voucher_template(db: AsyncSession = Depends(get_db), tenant: TenantContext = Depends(get_admin_tenant_context)):
    return _payload(await _operator(db, tenant))


@router.post("/voucher/preview")
async def preview_voucher_template(body: VoucherTemplatePreview, _: TenantContext = Depends(get_admin_tenant_context)):
    """Measure a draft without saving it. Returns the same shape either way so the
    editor can show the rendered text next to the reason it is refused."""
    error = templates.validation_error(body.template)
    measured = templates.preview(body.template)
    return {
        "valid": error is None,
        "error": error,
        "preview": {
            "text": measured.text,
            "encoding": measured.encoding,
            "segment_count": measured.segment_count,
            "character_count": measured.character_count,
        },
        "code_length_caveat": CODE_LENGTH_CAVEAT,
    }


@router.put("/voucher")
async def set_voucher_template(
    body: VoucherTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_active_operator),
):
    operator = await _operator(db, tenant)
    if body.template is None:
        operator.voucher_sms_template = None
    else:
        error = templates.validation_error(body.template)
        if error:
            raise HTTPException(status_code=400, detail=error)
        operator.voucher_sms_template = body.template.strip()
    await db.commit()
    await db.refresh(operator)
    logger.info(
        "voucher_sms_template_updated operator_id=%s reset_to_default=%s",
        operator.id, body.template is None,
    )
    return _payload(operator)
