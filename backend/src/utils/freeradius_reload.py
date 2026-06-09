import logging
import subprocess

logger = logging.getLogger("freeradius.reload")

_CONTAINERS = ["hotspot-freeradius", "hotspot-freeradius-secondary"]


def reload_freeradius_clients() -> None:
    """Send SIGHUP to FreeRADIUS containers to reload clients from the database.

    Designed to be called via run_in_executor after a router DB commit.
    Never raises — a failed reload is logged and ignored so the API call succeeds.
    """
    for container in _CONTAINERS:
        try:
            result = subprocess.run(
                ["docker", "kill", "--signal=SIGHUP", container],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.info("freeradius_reloaded container=%s", container)
            else:
                logger.warning(
                    "freeradius_reload_failed container=%s stderr=%s",
                    container,
                    result.stderr.strip(),
                )
        except Exception as exc:
            logger.warning("freeradius_reload_error container=%s error=%s", container, exc)
