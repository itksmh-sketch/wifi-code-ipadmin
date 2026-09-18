"""Voucher SMS message construction.

The process-wide SMSService/build_sms_service that used to live here was
removed once operator notifications moved to platform_notification_sms_credentials
(migration 039) and voucher delivery moved to per-operator resolution
(sms.provider_resolver). Nothing read it afterwards, and its Settings-backed
credentials could not reflect a change made through the admin UI.
"""
from __future__ import annotations

from src.db.models import Plan
from src.modules.sms import templates


def _format_duration(plan: Plan) -> str:
    # time / data / hybrid
    parts: list[str] = []
    if plan.type in ("time", "hybrid") and plan.duration_minutes:
        mins = int(plan.duration_minutes)
        if mins >= 60 and mins % 60 == 0:
            parts.append(f"{mins // 60} hrs")
        else:
            parts.append(f"{mins} mins")
    if plan.type in ("data", "hybrid") and plan.data_limit_mb:
        mb = int(plan.data_limit_mb)
        if mb >= 1024 and mb % 1024 == 0:
            parts.append(f"{mb // 1024} GB")
        elif mb >= 1024:
            parts.append(f"{mb / 1024:.1f} GB")
        else:
            parts.append(f"{mb} MB")
    return " + ".join(parts) if parts else "N/A"


def build_voucher_sms_message(*, code: str, plan: Plan, template: str | None = None, operator_name: str = "") -> str:
    """The purchase-confirmation text.

    ``template`` is the operator's own message (isp_operators.voucher_sms_template);
    None uses the platform default. It is rendered through sms.templates.render —
    str.format_map against a fixed allowlist, never f-string evaluation — and the
    saved template was validated to fit one segment for the default code length.
    """
    return templates.render(
        template or templates.DEFAULT_VOUCHER_SMS_TEMPLATE,
        code=code,
        plan_name=plan.name,
        validity=_format_duration(plan),
        operator_name=operator_name,
    )
