"""Voucher SMS message construction.

The process-wide SMSService/build_sms_service that used to live here was
removed once operator notifications moved to platform_notification_sms_credentials
(migration 039) and voucher delivery moved to per-operator resolution
(sms.provider_resolver). Nothing read it afterwards, and its Settings-backed
credentials could not reflect a change made through the admin UI.
"""
from __future__ import annotations

from src.db.models import Plan


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


def _truncate(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 1)].rstrip() + "…"


def build_voucher_sms_message(*, code: str, plan: Plan) -> str:
    # Must stay under 160 chars; we enforce by truncating plan name.
    duration = _format_duration(plan)
    plan_name = _truncate(plan.name, 24)
    msg = f"Your WiFi voucher: {code}. Plan: {plan_name}. Valid for {duration}. Connect at the login page. Enjoy!"
    if len(msg) <= 160:
        return msg
    # Tighten plan name further to respect 160-char constraint.
    plan_name = _truncate(plan.name, 12)
    msg = f"Your WiFi voucher: {code}. Plan: {plan_name}. Valid for {duration}. Connect at the login page. Enjoy!"
    return msg[:160]
