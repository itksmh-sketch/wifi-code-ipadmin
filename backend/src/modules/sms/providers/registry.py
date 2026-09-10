"""Construct a concrete `SMSProvider` from a provider key + decrypted credentials.

The single place that maps `operator_sms_credentials.provider` to an
implementation — used by `sms.provider_resolver`, the credential "test
connection" path (via `make_credentials_router`), and the per-operator
voucher-delivery send.

New providers are added here and nowhere else. Mirrors
`src/modules/payments/providers/registry.py`.
"""
from __future__ import annotations

from src.modules.sms.providers.africastalking import AfricasTalkingSMSProvider
from src.modules.sms.providers.arkesel import ArkeselSMSProvider
from src.modules.sms.providers.base import SMSProvider
from src.modules.sms.providers.hubtel import HubtelSMSProvider


def build_sms_provider(provider_key: str, credentials: dict) -> SMSProvider:
    if provider_key == "hubtel":
        return HubtelSMSProvider(
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"],
            sender_id=credentials["from"],
        )
    if provider_key == "africastalking":
        return AfricasTalkingSMSProvider(
            api_key=credentials["api_key"],
            username=credentials["username"],
            sender_id=credentials["from"],
        )
    if provider_key == "arkesel":
        return ArkeselSMSProvider(
            api_key=credentials["api_key"],
            sender_id=credentials["from"],
        )
    raise ValueError(f"Unsupported SMS provider: {provider_key}")
