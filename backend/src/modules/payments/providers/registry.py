"""Construct a concrete `PaymentProvider` from a provider key + decrypted
credentials. The single place that maps `operator_payment_credentials.provider`
to an implementation — used by `PaymentService.provider_for_transaction`, the
webhook route, and the credential "test connection" path.

New providers are added here and nowhere else. See
docs/payment-multi-provider-design.md.
"""
from __future__ import annotations

from src.modules.payments.providers.base import PaymentProvider
from src.modules.payments.providers.paystack import PaystackProvider


def build_payment_provider(
    provider_key: str, credentials: dict, *, callback_url: str | None = None
) -> PaymentProvider:
    if provider_key == "paystack":
        return PaystackProvider(
            secret_key=credentials["secret_key"],
            public_key=credentials["public_key"],
            webhook_secret=credentials.get("webhook_secret"),
            callback_url=callback_url,
        )
    if provider_key == "flutterwave":
        from src.modules.payments.providers.flutterwave import FlutterwaveProvider

        return FlutterwaveProvider(
            secret_key=credentials["secret_key"],
            public_key=credentials["public_key"],
            webhook_secret=credentials.get("webhook_secret"),
            callback_url=callback_url,
        )
    raise ValueError(f"Unsupported payment provider: {provider_key}")
