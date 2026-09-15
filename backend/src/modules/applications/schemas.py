from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, field_validator
import re

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Business description bounds. The apply page mirrors MESSAGE_MIN_LENGTH.
MESSAGE_MIN_LENGTH = 20
MESSAGE_MAX_LENGTH = 2000


def _normalize_email(v: str) -> str:
    v = v.strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email address")
    return v


class ApplicationSubmit(BaseModel):
    isp_name: str
    contact_name: str
    email: str
    phone: str
    region: str
    expected_sites: Optional[int] = None
    message: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("message")
    @classmethod
    def message_has_substance(cls, v: str) -> str:
        v = v.strip()
        if len(v) < MESSAGE_MIN_LENGTH:
            raise ValueError(
                f"Please tell us a little more about your business (at least {MESSAGE_MIN_LENGTH} characters)"
            )
        if len(v) > MESSAGE_MAX_LENGTH:
            raise ValueError(f"Please keep this under {MESSAGE_MAX_LENGTH} characters")
        return v

    @field_validator("isp_name")
    @classmethod
    def isp_name_min_length(cls, v: str) -> str:
        if len(v.strip()) < 3:
            raise ValueError("ISP name must be at least 3 characters")
        return v.strip()

    @field_validator("phone")
    @classmethod
    def ghana_phone(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        # Accept 233XXXXXXXXX or 0XXXXXXXXX (10 digits starting 02x/05x)
        if re.match(r"^233[0-9]{9}$", digits):
            return digits
        if re.match(r"^0[2-9][0-9]{8}$", digits):
            return "233" + digits[1:]
        raise ValueError("Phone must be a valid Ghana mobile number (e.g. 0244123456)")


class EmailCheckRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        return _normalize_email(v)


class EmailCheckResponse(BaseModel):
    # A bare boolean on purpose: never which record (admin/application) matched.
    available: bool


class ApplicationResponse(BaseModel):
    id: uuid.UUID
    isp_name: str
    contact_name: str
    email: str
    phone: str
    region: str
    expected_sites: Optional[int]
    message: Optional[str]
    status: str
    rejection_reason: Optional[str]
    isp_operator_id: Optional[uuid.UUID]
    created_at: datetime
    reviewed_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ApplicationReject(BaseModel):
    rejection_reason: str
