from __future__ import annotations

from functools import lru_cache

from src.config import get_settings
from src.modules.sms.service import SMSService, build_sms_service


@lru_cache()
def get_sms_service() -> SMSService:
    settings = get_settings()
    return build_sms_service(settings)

