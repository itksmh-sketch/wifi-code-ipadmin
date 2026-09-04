import logging

logger = logging.getLogger("freeradius.reload")


def reload_freeradius_clients() -> None:
    """No-op: dynamic_clients handles new NAS clients on first packet, no restart needed.

    Call sites are intentionally kept. To revert to restart-on-change (e.g. if
    dynamic_clients hits a production edge case), restore the subprocess restart
    logic here and update _DOCKER / _CONTAINERS as needed.
    """
    logger.debug("freeradius_reload_noop dynamic_clients handles new router clients on-demand")
