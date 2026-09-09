"""Operator bring-your-own payment credentials.

A thin adapter over the shared, catalog-schema-driven credentials core
(``src/modules/credentials/``). An operator configures one row per provider
(``operator_payment_credentials``, keyed ``(isp_operator_id, provider)``) and
marks one active; the field list for each provider comes from its
``provider_catalog`` credential_schema, so there is no per-provider code here.
See docs/payment-multi-provider-design.md.
"""
from __future__ import annotations

from src.db.models import OperatorPaymentCredential
from src.modules.credentials.router import make_credentials_router
from src.modules.payments.providers.registry import build_payment_provider

router = make_credentials_router(
    category="payment",
    prefix="/payment-credentials",
    tags=["payment-credentials"],
    model=OperatorPaymentCredential,
    # The test call never sends a webhook, so callback_url is irrelevant here.
    build_provider=lambda provider, credentials: build_payment_provider(
        provider, credentials, callback_url=None
    ),
    checklist_key="payment_configured",
)
