from __future__ import annotations
import uuid
from datetime import date, datetime
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
    # The trial as this operator actually has it (the platform's trial length
    # when they joined, plus any extension), so the progress bar is to scale.
    trial_started_at: Optional[datetime] = None
    trial_length_days: Optional[int] = None
    # The 1st of the month after the trial ends: when monthly billing starts.
    first_invoice_date: Optional[date] = None

    model_config = {"from_attributes": True}


class InvoiceLineItemResponse(BaseModel):
    kind: str
    description: str
    quantity: Decimal
    unit_price_ghs: Decimal
    amount_ghs: Decimal

    model_config = {"from_attributes": True}


class ReceiptResponse(BaseModel):
    """How a paid invoice was paid."""
    method: str
    reference: Optional[str]
    paid_on: Optional[date]
    amount_ghs: Decimal


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
    line_items: List[InvoiceLineItemResponse] = []
    receipt: Optional[ReceiptResponse] = None

    model_config = {"from_attributes": True}


class InvoiceParty(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None


class InvoiceDetailResponse(InvoiceResponse):
    """One invoice with everything its printed copy shows."""
    bill_to: InvoiceParty
    issuer: InvoiceParty


class PayInvoiceResponse(BaseModel):
    redirect_url: str
    invoice_id: uuid.UUID
