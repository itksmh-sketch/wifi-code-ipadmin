"""Operator bring-your-own SMS-gateway credentials.

A thin adapter over the shared, catalog-schema-driven credentials core
(``src/modules/credentials/``) — the same five-endpoint CRUD as
``payment-credentials``. One row per provider (``operator_sms_credentials``,
keyed ``(isp_operator_id, provider)``), one active; the field list per provider
comes from its ``provider_catalog`` credential_schema.

SMS is not an onboarding-checklist step, so ``checklist_key`` is None. The
platform-gateway SMS option (``africastalking_platform``) is not operator-
configurable and never reaches here — its catalog row is ``configured_by:
platform_admin``, which the shared core rejects with a 400.
"""
from __future__ import annotations

from src.db.models import OperatorSMSCredential
from src.modules.credentials.router import make_credentials_router
from src.modules.sms.providers.registry import build_sms_provider

router = make_credentials_router(
    category="sms",
    prefix="/sms-credentials",
    tags=["sms-credentials"],
    model=OperatorSMSCredential,
    build_provider=build_sms_provider,
    checklist_key=None,
)
