"""Audit trail: see service (writing), redact (what a row may say) and context
(who is acting, from where). Migration 055 has the table and its rules."""
from src.modules.audit.context import (
    OPERATOR_USER,
    PLATFORM_OWNER,
    SYSTEM,
    Actor,
    RequestInfo,
    audited,
    bind_actor,
    current_actor,
    current_request_info,
)
from src.modules.audit import fields
from src.modules.audit.redact import diff, snapshot
from src.modules.audit.service import record, record_detached

__all__ = [
    "OPERATOR_USER",
    "PLATFORM_OWNER",
    "SYSTEM",
    "Actor",
    "RequestInfo",
    "audited",
    "bind_actor",
    "current_actor",
    "current_request_info",
    "diff",
    "fields",
    "record",
    "record_detached",
    "snapshot",
]
