from apscheduler.schedulers.background import BackgroundScheduler
from src.jobs.voucher_expiry import expire_vouchers
import logging

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def start_scheduler():
    """Start the background scheduler with the voucher expiry job."""
    scheduler.add_job(
        expire_vouchers,
        "interval",
        minutes=5,
        id="voucher_expiry",
        name="Expire overdue vouchers",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Background scheduler started — voucher expiry job runs every 5 minutes")


def stop_scheduler():
    """Shutdown the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Background scheduler stopped")
