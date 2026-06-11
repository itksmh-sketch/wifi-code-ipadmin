from arq import cron
from arq.connections import RedisSettings

from src.config import get_settings
from src.jobs.coa_retry import retry_failed_coa_events
from src.jobs.collect_router_metrics import collect_router_metrics
from src.jobs.payment_reconciliation import run_payment_reconciliation
from src.jobs.router_health import check_router_health
from src.jobs.voucher_expiry import expire_vouchers
from src.jobs.invoice_generation import generate_monthly_invoices
from src.jobs.trial_expiry import handle_trial_expiry
from src.jobs.billing_enforcement import enforce_billing
from src.jobs.wireguard_status import check_wireguard_tunnels
from src.logging_config import configure_logging
from src.modules.webhooks.processor import process_webhook_event
import structlog

settings = get_settings()
configure_logging()
logger = structlog.get_logger(__name__)


def _redis_settings() -> RedisSettings:
    redis_url = settings.redis_url
    if redis_url.startswith("redis://"):
        host_port = redis_url.replace("redis://", "", 1)
        host, port = host_port.split(":")
        return RedisSettings(host=host, port=int(port))
    return RedisSettings()


class WorkerSettings:
    redis_settings = _redis_settings()
    functions = [process_webhook_event, run_payment_reconciliation, expire_vouchers, retry_failed_coa_events, check_router_health, collect_router_metrics, generate_monthly_invoices, handle_trial_expiry, enforce_billing, check_wireguard_tunnels]
    cron_jobs = [
        cron(check_router_health, minute={0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56, 58}),
        cron(check_wireguard_tunnels, minute={0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56, 58}),
        cron(expire_vouchers, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(retry_failed_coa_events, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(run_payment_reconciliation, minute={0, 10, 20, 30, 40, 50}),
        cron(collect_router_metrics, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(generate_monthly_invoices, hour=0, minute=0),
        cron(handle_trial_expiry, hour=6, minute=0),
        cron(enforce_billing, hour=8, minute=0),
    ]


logger.info("worker_functions_registered", functions=[fn.__name__ for fn in WorkerSettings.functions])
