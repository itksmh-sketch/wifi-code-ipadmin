"""The operator Billing -> SMS Usage tab's delivery column: the `delivery`
field /billing/sms-usage adds from sms_messages. Pure unit tests: the
database and the tenant are stubbed."""
import types
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from src.app import app
from src.db.base import get_db
from src.middleware.auth import get_admin_tenant_context
from src.modules.billing.routes import _delivery

ACCEPTED = datetime(2026, 10, 5, 14, 6, 22, tzinfo=timezone.utc)
SENT = datetime(2026, 10, 5, 15, 0, 34, tzinfo=timezone.utc)


def msg(status=None, sent_at=None, final_at=None):
    return types.SimpleNamespace(provider_status=status, accepted_at=ACCEPTED, provider_sent_at=sent_at,
                                 final_status_at=final_at)


@pytest.mark.parametrize("m,state", [
    (msg("DELIVERED", SENT, SENT), "delivered"),
    (msg("delivered", SENT, SENT), "delivered"),
    (msg("NOT_DELIVERED", SENT, SENT), "not_delivered"),
    (msg("EXPIRED", None, SENT), "not_delivered"),
    (msg("SUBMITTED", SENT), "sent_to_network"),
    (msg("SUBMITTED", None), "waiting"),
    (msg(None, None), "waiting"),
])
def test_delivery_state(m, state):
    assert _delivery(m)["state"] == state


def test_network_delay_is_the_gap_between_acceptance_and_dispatch():
    # The 2026-10-05 purchase SMS: 54 min 12 s inside Arkesel.
    d = _delivery(msg("DELIVERED", SENT, SENT))
    assert d["network_delay_seconds"] == 3252
    assert d["sent_to_network_at"] == SENT.isoformat() and d["accepted_at"] == ACCEPTED.isoformat()
    assert _delivery(msg("SUBMITTED", None))["network_delay_seconds"] is None


def test_untracked_record_has_no_delivery():
    assert _delivery(None) is None


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value

    def all(self):
        return self.value


class _Db:
    def __init__(self, rows):
        self.answers = [len(rows), rows]
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self.answers.pop(0))


@pytest.mark.asyncio
async def test_endpoint_returns_delivery_per_record():
    op = uuid.uuid4()

    def rec(i):
        return types.SimpleNamespace(id=uuid.uuid4(), sent_at=ACCEPTED, segment_count=1,
                                     rate_ghs_per_segment=Decimal("0.30"), amount_ghs=Decimal("0.30"))

    rows = [(rec(1), None, None, msg("DELIVERED", SENT, SENT)), (rec(2), "INV-1", ACCEPTED, None)]
    db = _Db(rows)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_admin_tenant_context] = lambda: types.SimpleNamespace(isp_operator_id=op)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://ip-admin.example.test") as client:
            res = await client.get("/api/v1/billing/sms-usage")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_admin_tenant_context, None)
    assert res.status_code == 200
    first, second = res.json()["records"]
    assert first["delivery"]["state"] == "delivered" and first["delivery"]["network_delay_seconds"] == 3252
    assert second["delivery"] is None and second["invoice_number"] == "INV-1"
    # The join is tenant-scoped and matched on the platform gateway's message id.
    sql = str(db.statements[1].compile(compile_kwargs={"literal_binds": False}))
    assert "LEFT OUTER JOIN sms_messages" in sql
    assert "sms_messages.provider_reference = sms_usage_records.provider_reference" in sql
    assert "sms_messages.isp_operator_id = sms_usage_records.isp_operator_id" in sql
