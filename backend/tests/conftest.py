"""Safety guard for this repo's integration tests.

The integration suites (test_multi_tenancy, test_billing, test_branding,
test_plan_dedup, test_router_setup, and the shared test_multi_tenant_security)
POST real rows — operators, admins, towns, sites, routers, plans, invoices — to
whatever ``TEST_BASE_URL`` points at, and have **no teardown**. On this
single-server deployment ``http://localhost:8000`` (the default when
``TEST_BASE_URL`` is unset) IS production, so a plain ``pytest tests/`` writes
junk into the live database. This has happened — 64 fake operators, once.

An integration run is now impossible without a deliberate opt-in:

  1. ``ALLOW_INTEGRATION_TESTS=1`` must be set (absent by default). This is the
     real gate — it would have stopped every accidental run.
  2. Independently, the effective base URL must not name a known production host
     and must match a local / throwaway / CI pattern.

Either failing → pytest exits at collection time (returncode 2) with a loud
banner. Pure unit-test runs (no integration module collected) are unaffected.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

_OPT_IN_ENV = "ALLOW_INTEGRATION_TESTS"
_BASE_URL_ENV = "TEST_BASE_URL"
_DEFAULT_BASE_URL = "http://localhost:8000"

# Substrings that must never appear in the target URL.
_PRODUCTION_HOSTS = ("34.122.11.114", "ip-admin.duckdns.org")

# The target must look like a local / disposable / CI host. localhost is allowed
# here on purpose — it is NOT a sufficient guard on its own (that is exactly how
# production was hit), which is why the opt-in flag above is mandatory. Widen
# this (with a note) if you add a real dedicated CI target.
_SAFE_BASE_URL = re.compile(
    r"^https?://("
    r"localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|host\.docker\.internal|"
    r"[a-z0-9][a-z0-9-]*\.(test|local|localhost)|"
    r"test[-.][a-z0-9-]+"
    r")(:\d+)?(/.*)?$",
    re.IGNORECASE,
)


def _module_of(item):
    try:
        return item.module
    except Exception:
        return None


def _is_integration_module(mod) -> bool:
    # Detection is by convention: every integration test module either defines a
    # module-level BASE_URL or imports the shared _request helper from
    # test_multi_tenant_security, so one of these names is in its namespace.
    #
    # !!! IMPORTANT FOR FUTURE INTEGRATION TESTS !!!
    # A new file that talks to a live server MUST either (a) follow that same
    # convention — `from test_multi_tenant_security import _request` (or BASE_URL)
    # at module level — or (b) be added to this detection logic explicitly.
    # If it does neither, this guard SILENTLY DOES NOT PROTECT IT, and a bare
    # `pytest tests/` will run it against whatever TEST_BASE_URL points at
    # (production, by default) with no cleanup. When in doubt, import _request.
    return mod is not None and (hasattr(mod, "_request") or hasattr(mod, "BASE_URL"))


def _effective_base_url() -> str:
    # The value the tests will actually use: test_multi_tenant_security.BASE_URL
    # is bound at import time from the env, so read it back if the module is
    # loaded; otherwise re-derive from the env with the same default.
    helper = sys.modules.get("test_multi_tenant_security")
    if helper is not None and isinstance(getattr(helper, "BASE_URL", None), str):
        return helper.BASE_URL
    return os.getenv(_BASE_URL_ENV, _DEFAULT_BASE_URL)


def _guard_failures(base_url: str) -> list[str]:
    problems: list[str] = []
    if os.getenv(_OPT_IN_ENV) != "1":
        problems.append(
            f"{_OPT_IN_ENV} is not set to '1'. Integration tests write to the live "
            f"database and never clean up. Only set {_OPT_IN_ENV}=1 when "
            f"{_BASE_URL_ENV} points at a throwaway database — never on the "
            f"production VM (localhost included: on this box localhost IS prod)."
        )
    low = base_url.lower()
    for host in _PRODUCTION_HOSTS:
        if host in low:
            problems.append(
                f"{_BASE_URL_ENV}={base_url!r} names a known production host ({host})."
            )
    if not _SAFE_BASE_URL.match(base_url.strip()):
        problems.append(
            f"{_BASE_URL_ENV}={base_url!r} does not match the local/CI safe pattern "
            f"(localhost, 127.0.0.1, *.test, *.local, test-*). If this really is "
            f"disposable, widen _SAFE_BASE_URL in tests/conftest.py."
        )
    return problems


def pytest_collection_modifyitems(config, items):
    integration = [it for it in items if _is_integration_module(_module_of(it))]
    if not integration:
        return  # unit-only run — nothing to guard

    base_url = _effective_base_url()
    failures = _guard_failures(base_url)
    if failures:
        pytest.exit(
            "\n".join(
                [
                    "",
                    "=" * 74,
                    "INTEGRATION TESTS BLOCKED — safety guard tripped (tests/conftest.py)",
                    "=" * 74,
                    *(f"  ✗ {f}" for f in failures),
                    "",
                    f"Target would have been: {base_url}",
                    f"{len(integration)} integration tests create operators/routers/etc. "
                    "with NO teardown.",
                    "=" * 74,
                ]
            ),
            returncode=2,
        )

    sys.stderr.write(
        "\n" + "!" * 74 + "\n"
        f"  RUNNING {len(integration)} INTEGRATION TESTS against {base_url}\n"
        f"  ({_OPT_IN_ENV}=1 is set) — this WILL write rows to that DB with no cleanup.\n"
        + "!" * 74 + "\n\n"
    )
