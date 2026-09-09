from functools import lru_cache

from src.modules.payments.service import PaymentService


@lru_cache()
def get_payment_service() -> PaymentService:
    # PaymentService is stateless: payment provider and SMS gateway are both
    # resolved per-transaction from the operator's active credential rows.
    return PaymentService()
