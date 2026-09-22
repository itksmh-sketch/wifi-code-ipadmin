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
    # The *access* axis, separate from billing_status on purpose: an operator can
    # be billing_status='active' while status='suspended' (a manual suspension),
    # and billing_status='past_due' while still fully able to trade. Only
    # `status` decides whether the write guards bite, so only `status` can drive
    # the dashboard banner. suspension_reason distinguishes "pay your invoice"
    # from a manual suspension a payment will not lift.
    account_status: str
    is_suspended: bool
    suspension_reason: Optional[str]

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
