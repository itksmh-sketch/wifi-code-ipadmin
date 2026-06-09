from __future__ import annotations
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel


class BillingStatusResponse(BaseModel):
    billing_status: str
    trial_ends_at: Optional[datetime]
    trial_days_remaining: Optional[int]
    has_outstanding_invoice: bool
    outstanding_amount_ghs: Optional[Decimal]

    model_config = {"from_attributes": True}


class InvoiceResponse(BaseModel):
    id: uuid.UUID
    invoice_number: str
    period_start: datetime
    period_end: datetime
    amount_ghs: Decimal
    status: str
    issued_at: Optional[datetime]
    due_at: Optional[datetime]
    paid_at: Optional[datetime]
    payment_reference: Optional[str]
    paystack_payment_url: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class PayInvoiceResponse(BaseModel):
    redirect_url: str
    invoice_id: uuid.UUID
