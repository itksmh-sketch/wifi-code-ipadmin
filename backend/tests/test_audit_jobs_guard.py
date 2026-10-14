"""Audit coverage for the worker: every job is audited or deliberately exempt.

The route guard (test_audit_guard.py) covers what people do. This covers what
the system does on its own. A job is audited when it, or the service it runs
through, writes a row (actor "system") for each decision it makes. A job is
exempt when it only watches (health, metrics, delivery status) or carries out
a decision whose row was already written (a disconnect for a voucher whose
expiry row exists); those are recorded in their own tables.

What fails:
  * a job registered with the worker that is in neither list;
  * a list entry naming a job the worker no longer runs;
  * an audited job none of whose listed modules calls audit.record_system
    (or its fail-open form, for customer-facing paths).

Pure unit test: no server, no DB, no Redis.
"""
from __future__ import annotations

import importlib
import inspect

from src.jobs.worker import WorkerSettings

# job name -> the modules whose audit.record calls write its rows.
AUDITED_JOBS = {
    # payment.status_change, in the payment service every trigger shares.
    "process_webhook_event": ("src.modules.payments.service",),
    "run_payment_reconciliation": ("src.jobs.payment_reconciliation", "src.modules.payments.service"),
    # voucher.expire / voucher.exhaust.
    "expire_vouchers": ("src.modules.vouchers.engine",),
    # invoice.issue.
    "generate_monthly_invoices": ("src.modules.billing.service",),
    # operator.trial_end, invoice.issue.
    "handle_trial_expiry": ("src.jobs.trial_expiry", "src.modules.billing.service"),
    # invoice.overdue, operator.suspend.
    "enforce_billing": ("src.jobs.billing_enforcement",),
    # router.wireguard_remove for a removal finished on retry. The tunnel
    # status refresh it also does is monitoring.
    "check_wireguard_tunnels": ("src.jobs.wireguard_status",),
    # router.cookie_policy_applied: live config written on a router. Its
    # disconnects carry out voucher decisions already recorded, or the end of
    # a cancelled operator's wind-down (operator.hotspot_service_ended).
    "reconcile_hotspot": ("src.jobs.reconcile_hotspot",),
    # audit.purge, one row per batch it deletes, in the same transaction.
    "purge_audit_log": ("src.jobs.audit_retention",),
    # operator.hotspot_service_ended, once per wind-down, with the counts.
    "end_hotspot_service": ("src.jobs.hotspot_service_end",),
}

EXEMPT_JOBS = {
    "check_router_health": "marks routers online/offline: monitoring",
    "collect_router_metrics": "stores router metrics: monitoring",
    "retry_failed_coa_events": "retries disconnects already decided; each attempt is a coa_events row",
    "check_exhausted_sessions": "disconnects sessions of vouchers whose expiry row exists; session terminate_cause records it",
    "session_watchdog": "closes session rows that stopped reporting: accounting bookkeeping",
    "reconcile_sms_usage": "compares SMS balances and alerts; changes no record",
    "send_voucher_sms": "texts a paid voucher; sms_messages records delivery",
    "refresh_sms_status": "reads SMS delivery status; sms_messages records it",
    "poll_sms_statuses": "reads SMS delivery status; sms_messages records it",
}


def _job_names() -> set[str]:
    names = set()
    for fn in WorkerSettings.functions:
        coroutine = getattr(fn, "coroutine", fn)
        names.add(getattr(coroutine, "__name__", None) or getattr(fn, "name"))
    for job in WorkerSettings.cron_jobs:
        names.add(job.coroutine.__name__)
    return names


def test_lists_do_not_overlap():
    assert not (set(AUDITED_JOBS) & set(EXEMPT_JOBS))


def test_every_job_is_classified():
    unclassified = sorted(_job_names() - set(AUDITED_JOBS) - set(EXEMPT_JOBS))
    assert not unclassified, (
        "Worker job(s) with no audit decision. Write a system row for each decision "
        f"they make and list them in AUDITED_JOBS, or list them in EXEMPT_JOBS with a reason: {unclassified}"
    )


def test_no_stale_entries():
    assert not (set(AUDITED_JOBS) | set(EXEMPT_JOBS)) - _job_names()


def test_audited_jobs_write_rows():
    for job, modules in AUDITED_JOBS.items():
        sources = [inspect.getsource(importlib.import_module(name)) for name in modules]
        # record_system (fail-closed) or record_system_fail_open (customer-facing paths).
        assert any("audit.record_system" in source for source in sources), f"{job}: no audit.record_system in {modules}"
