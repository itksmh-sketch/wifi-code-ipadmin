from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


class PaymentMethod(str, Enum):
    MTN_MOMO = "mtn_momo"
    VODAFONE_CASH = "vodafone_cash"
    AIRTELTIGO = "airteltigo"
    CARD = "card"


class PaymentProviderName(str, Enum):
    MTN = "mtn"
    VODAFONE = "vodafone"
    AIRTELTIGO = "airteltigo"
    PAYSTACK = "paystack"


# provider_state marker for "we could not reach the provider this time".
# It is NOT a payment outcome: the charge is still whatever it was. Callers
# must treat it as "no new information" and leave the stored transaction
# state alone rather than overwriting it with the placeholder result.
PROVIDER_UNREACHABLE_STATE = "provider_unreachable"

# The failure_reason the reconciliation job writes when it gives up on a
# payment after two hours with no verdict from the provider. Ours, never a
# provider's text: it marks the one kind of failed payment that a success
# confirmed later may still turn into a sale (resolve_successful_payment), and
# that the job keeps re-checking for LATE_RECHECK_WINDOW.
TIMEOUT_FAILURE_REASON = "timeout"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    REFUNDED = "refunded"
    REVERSED = "reversed"


class PaymentNextAction(str, Enum):
    NONE = "none"
    WAIT = "wait"
    ENTER_OTP = "enter_otp"
    ENTER_PHONE = "enter_phone"
    ENTER_PIN = "enter_pin"
    ENTER_BIRTHDAY = "enter_birthday"
    ENTER_ADDRESS = "enter_address"
    OPEN_URL = "open_url"


@dataclass(slots=True)
class PaymentInitiationResult:
    provider_reference: Optional[str]
    status: PaymentStatus
    failure_reason: Optional[str] = None
    next_action: PaymentNextAction = PaymentNextAction.NONE
    provider_state: Optional[str] = None
    display_message: Optional[str] = None
    provider_payload: Optional[dict] = None
    payment_channel: Optional[str] = None
    authorization_url: Optional[str] = None


@dataclass(slots=True)
class PaymentVerificationResult:
    status: PaymentStatus
    amount_ghs: Decimal
    provider_reference: Optional[str] = None
    failure_reason: Optional[str] = None
    next_action: PaymentNextAction = PaymentNextAction.NONE
    provider_state: Optional[str] = None
    display_message: Optional[str] = None
    provider_payload: Optional[dict] = None
    payment_channel: Optional[str] = None


@dataclass(slots=True)
class PaymentWebhookResult:
    internal_reference: str
    status: PaymentStatus
    provider_reference: Optional[str] = None
    provider_state: Optional[str] = None
    display_message: Optional[str] = None
    provider_payload: Optional[dict] = None
    payment_channel: Optional[str] = None
