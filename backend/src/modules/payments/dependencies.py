from functools import lru_cache

from src.config import get_settings
from src.modules.payments.providers.airteltigo import AirtelTigoMockProvider
from src.modules.payments.providers.mtn import MTNMoMoProvider
from src.modules.payments.providers.paystack import PaystackProvider
from src.modules.payments.providers.vodafone import VodafoneCashMockProvider
from src.modules.payments.service import PaymentService
from src.modules.payments.types import PaymentProviderName
from src.modules.sms.dependencies import get_sms_service


@lru_cache()
def get_payment_service() -> PaymentService:
    settings = get_settings()
    return PaymentService(
        mtn_provider=MTNMoMoProvider(settings),
        vodafone_provider=VodafoneCashMockProvider(settings),
        airteltigo_provider=AirtelTigoMockProvider(settings),
        paystack_provider=PaystackProvider(settings),
        sms_service=get_sms_service(),
    )


def provider_name_for_method(method: str) -> str:
    if method == "mtn_momo":
        return PaymentProviderName.MTN.value
    if method == "vodafone_cash":
        return PaymentProviderName.VODAFONE.value
    if method == "airteltigo":
        return PaymentProviderName.AIRTELTIGO.value
    return PaymentProviderName.PAYSTACK.value
