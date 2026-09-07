"""Pure verification rules for a platform-billing charge.

No database, no network, no framework — a payload and an expected amount in,
a verdict out. Every branch is exhaustively testable, and the webhook handler
reduces to "look up the invoice -> verify -> act".

This is the code that decides whether money settles an invoice, so it is kept
deliberately dumb and total: every rejection has a named reason, and anything
not explicitly accepted is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional

# The platform bills Ghanaian operators; a charge in anything else did not come
# from a flow we initiated.
EXPECTED_CURRENCY = "GHS"

# Above this multiple of the invoice an overpayment is still accepted — refusing
# would leave someone who overpaid suspended — but logged loudly as a likely
# fat-finger rather than passing silently.
OVERPAYMENT_WARN_MULTIPLE = Decimal("2")


class ChargeRejection(str, Enum):
    NOT_SUCCESS = "not_success"
    MISSING_AMOUNT = "missing_amount"
    MALFORMED_AMOUNT = "malformed_amount"
    WRONG_CURRENCY = "wrong_currency"
    UNDERPAID = "underpaid"


@dataclass(frozen=True)
class ChargeVerdict:
    accepted: bool
    rejection: Optional[ChargeRejection] = None
    reason: str = ""
    charged_pesewas: Optional[int] = None
    expected_pesewas: Optional[int] = None
    currency: str = ""
    # True when accepted for more than the invoice — worth a loud log, not a refusal.
    overpaid: bool = False
    # True when the overpayment is large enough to look like a mistake.
    overpaid_suspiciously: bool = False

    @property
    def charged_ghs(self) -> Optional[Decimal]:
        if self.charged_pesewas is None:
            return None
        return (Decimal(self.charged_pesewas) / Decimal("100")).quantize(Decimal("0.01"))


def _coerce_pesewas(raw: Any) -> Optional[int]:
    """Paystack sends an integer minor unit. Accept only something exactly integral."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if value != value.to_integral_value():
        return None
    try:
        return int(value)
    except (OverflowError, ValueError):
        return None


def verify_charge(payload: dict, *, expected_pesewas: int) -> ChargeVerdict:
    """Decide whether this webhook payload settles an invoice of `expected_pesewas`.

    Rejections are terminal: the same payload will always fail the same way, so a
    caller should record the rejection for a human rather than ask for a retry.
    """
    event = payload.get("event")
    if event != "charge.success":
        return ChargeVerdict(
            accepted=False,
            rejection=ChargeRejection.NOT_SUCCESS,
            reason=f"Event is {event!r}, not charge.success.",
            expected_pesewas=expected_pesewas,
        )

    data = payload.get("data") or {}

    currency = str(data.get("currency") or "").strip().upper()
    if currency != EXPECTED_CURRENCY:
        return ChargeVerdict(
            accepted=False,
            rejection=ChargeRejection.WRONG_CURRENCY,
            reason=f"Charge currency is {currency or 'unset'!r}, expected {EXPECTED_CURRENCY}.",
            expected_pesewas=expected_pesewas,
            currency=currency,
        )

    if "amount" not in data or data.get("amount") is None:
        return ChargeVerdict(
            accepted=False,
            rejection=ChargeRejection.MISSING_AMOUNT,
            reason="Charge carries no amount.",
            expected_pesewas=expected_pesewas,
            currency=currency,
        )

    charged = _coerce_pesewas(data.get("amount"))
    if charged is None:
        return ChargeVerdict(
            accepted=False,
            rejection=ChargeRejection.MALFORMED_AMOUNT,
            reason=f"Charge amount {data.get('amount')!r} is not a whole number of pesewas.",
            expected_pesewas=expected_pesewas,
            currency=currency,
        )

    if charged < expected_pesewas:
        # The exploit this whole phase exists to close: without it, any
        # successful charge of any size settled the invoice in full.
        return ChargeVerdict(
            accepted=False,
            rejection=ChargeRejection.UNDERPAID,
            reason=(
                f"Charged GHS {Decimal(charged) / 100:.2f} against an invoice of "
                f"GHS {Decimal(expected_pesewas) / 100:.2f}."
            ),
            charged_pesewas=charged,
            expected_pesewas=expected_pesewas,
            currency=currency,
        )

    overpaid = charged > expected_pesewas
    suspicious = bool(
        overpaid
        and expected_pesewas > 0
        and Decimal(charged) > Decimal(expected_pesewas) * OVERPAYMENT_WARN_MULTIPLE
    )
    return ChargeVerdict(
        accepted=True,
        charged_pesewas=charged,
        expected_pesewas=expected_pesewas,
        currency=currency,
        overpaid=overpaid,
        overpaid_suspiciously=suspicious,
    )
