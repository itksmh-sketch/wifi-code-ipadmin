from functools import lru_cache

from src.modules.payments.service import PaymentService
from src.modules.sms.dependencies import get_sms_service


@lru_cache()
def get_payment_service() -> PaymentService:
    # Payment providers are resolved per-transaction from the operator's active
    # operator_payment_credentials row (see PaymentService.provider_for_transaction),
    # so nothing provider-specific is constructed here.
    return PaymentService(sms_service=get_sms_service())
