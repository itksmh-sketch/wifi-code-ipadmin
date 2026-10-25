"""Throwaway databases built the way a fresh migration chain builds them, and a
runner that gives every database-backed suite its own.

Why: a schema-only copy of production has the tables but none of the rows the
migrations seed (044/056 notification templates, 014/049 platform settings,
the provider catalog, 010's tenant-zero), and suites that share one database
leave operators behind that later suites trip over (the seed's live-database
guard refuses). Both showed up as test failures that were not regressions.
Here the template is made by `alembic upgrade head` on an empty database —
exactly what a new environment gets — and every suite runs on its own clone of
it, in its own process (the flow suites cache an engine per event loop, so two
modules in one process fail with "attached to a different loop").

Usage, from backend/, in a container whose only reachable Postgres is a
throwaway server (never production):

    python tests/throwaway_db.py build --server postgresql://user:pw@127.0.0.1:5432
    python tests/throwaway_db.py run   --server postgresql://user:pw@127.0.0.1:5432 \\
        [--redis-url redis://127.0.0.1:6380] [--timeout 900] [--keep-databases] [tests/test_x_flow.py ...]

`run` rebuilds the template first unless --reuse-template is given, then runs
the unit suites in one process and each database suite on a fresh clone, and
exits non-zero if anything failed. The app settings (ENCRYPTION_KEY, the JWT
secrets, …) come from the environment as usual — use throwaway values.

Refusals (exit 2, nothing touched): a server that holds any database whose name
lacks "throwaway" (production's hotspot_db, for one), or a production host name.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql

BACKEND = Path(__file__).resolve().parents[1]
TESTS = BACKEND / "tests"
TEMPLATE = "flow_template_throwaway"
SYSTEM_DATABASES = {"postgres", "template0", "template1"}
PRODUCTION_HOSTS = ("35.223.244.216", "ip-admin.duckdns.org", "ip-admin-portal.duckdns.org", "34.122.11.114")
# Unreachable on purpose: suites that don't ask for Redis must not depend on it.
DEAD_REDIS = "redis://127.0.0.1:1"
# Migration inputs that would seed rows a fresh environment doesn't have.
SEED_INPUT_VARS = ("PLATFORM_OWNER_EMAIL", "PLATFORM_OWNER_PASSWORD", "PAYSTACK_PUBLIC_KEY",
                   "PAYSTACK_SECRET_KEY", "PAYSTACK_WEBHOOK_SECRET")
# Suites that talk to a live server over HTTP (conftest refuses them here).
LIVE_SERVER_MARKERS = re.compile(r"^BASE_URL\s*=|^from test_multi_tenant_security import", re.M)
DB_VAR = re.compile(r"\b([A-Z][A-Z0-9_]*_DATABASE_URL)\b")
REDIS_VAR = re.compile(r"\b([A-Z][A-Z0-9_]*_REDIS_URL)\b")


class Refused(SystemExit):
    def __init__(self, why: str):
        print(f"\n!!! throwaway_db refused: {why}\n", file=sys.stderr)
        super().__init__(2)


def _with_db(server: str, db: str, driver: str = "postgresql") -> str:
    parts = urlsplit(server)
    return urlunsplit((driver, parts.netloc, f"/{db}", "", ""))


@contextmanager
def _cursor(server: str, db: str = "postgres"):
    # Autocommit, and no `with conn:` — psycopg2 wraps that in a transaction,
    # where CREATE/DROP DATABASE are not allowed.
    conn = psycopg2.connect(_with_db(server, db))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def check_server(server: str) -> None:
    """Refuse anything that could be production, before touching the server."""
    host = urlsplit(server).hostname or ""
    if any(h in server for h in PRODUCTION_HOSTS):
        raise Refused(f"{host} is a production host")
    with _cursor(server) as cur:
        cur.execute("SELECT datname FROM pg_database")
        others = sorted(n for (n,) in cur.fetchall() if n not in SYSTEM_DATABASES and "throwaway" not in n)
    if others:
        raise Refused(f"the server also holds {', '.join(others)}; only a throwaway server is allowed")


def drop_database(server: str, name: str) -> None:
    if "throwaway" not in name:
        raise Refused(f"database name {name!r} must contain 'throwaway'")
    with _cursor(server) as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _recreate(server: str, name: str, template: str | None = None) -> None:
    drop_database(server, name)
    with _cursor(server) as cur:
        if template:
            cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(name), sql.Identifier(template)))
        else:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _db_env(server: str, name: str) -> dict[str, str]:
    return {"DATABASE_URL": _with_db(server, name, "postgresql+asyncpg"),
            "SYNC_DATABASE_URL": _with_db(server, name)}


def verify_fresh(server: str, name: str) -> None:
    """What a fresh chain must contain: every default template, the seeded
    settings and catalog, tenant-zero as the only operator, and no people."""
    sys.path.insert(0, str(BACKEND))
    from src.modules.notifications.template_catalog import DEFAULTS

    with _cursor(server, name) as cur:
        cur.execute("SELECT event_type, channel FROM platform_notification_templates")
        templates = {tuple(r) for r in cur.fetchall()}
        expected = {(d.event, d.channel) for d in DEFAULTS.values()}
        cur.execute("SELECT slug FROM isp_operators ORDER BY slug")
        operators = [r[0] for r in cur.fetchall()]
        counts = {}
        for table in ("platform_settings", "provider_catalog", "platform_owners", "admin_users", "routers", "audit_log"):
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            counts[table] = cur.fetchone()[0]
    problems = []
    if templates != expected:
        problems.append(f"templates missing {sorted(expected - templates)} extra {sorted(templates - expected)}")
    if operators != ["tenant-zero"]:
        problems.append(f"operators {operators} (expected only tenant-zero)")
    for table in ("platform_settings", "provider_catalog"):
        if not counts[table]:
            problems.append(f"{table} is empty")
    for table in ("platform_owners", "admin_users", "routers", "audit_log"):
        if counts[table]:
            problems.append(f"{table} has {counts[table]} rows")
    if problems:
        raise SystemExit("template is not a fresh chain: " + "; ".join(problems))
    print(f"template ok: {len(templates)} templates, settings {counts['platform_settings']}, "
          f"catalog {counts['provider_catalog']}, operators {operators}")


def build_template(server: str) -> None:
    check_server(server)
    _recreate(server, TEMPLATE)
    with _cursor(server, TEMPLATE) as cur:
        # Alembic would create version_num as varchar(32); revision ids are
        # longer (026_…), so the chain dies at 026 without this. Same width and
        # constraint name as production.
        cur.execute("CREATE TABLE alembic_version (version_num varchar(255) NOT NULL, "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")
    env = {k: v for k, v in os.environ.items() if k not in SEED_INPUT_VARS}
    env.update(_db_env(server, TEMPLATE))
    started = time.monotonic()
    res = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env,
                         capture_output=True, text=True)
    if res.returncode:
        print(res.stdout[-4000:], res.stderr[-4000:], file=sys.stderr)
        raise SystemExit("alembic upgrade head failed on the template")
    print(f"template built by alembic upgrade head in {time.monotonic() - started:.0f}s")
    verify_fresh(server, TEMPLATE)


def fresh_database(server: str, name: str) -> dict[str, str]:
    """A new clone of the template; returns DATABASE_URL / SYNC_DATABASE_URL."""
    _recreate(server, name, TEMPLATE)
    return _db_env(server, name)


def classify(files: list[Path]) -> tuple[list[Path], list[tuple[Path, list[str], list[str]]]]:
    """(unit files, [(db file, its *_DATABASE_URL vars, its *_REDIS_URL vars)])."""
    unit, db = [], []
    for f in files:
        text = f.read_text()
        if LIVE_SERVER_MARKERS.search(text):
            continue
        db_vars = sorted(set(DB_VAR.findall(text)) - {"SYNC_DATABASE_URL"})
        if db_vars:
            db.append((f, db_vars, sorted(set(REDIS_VAR.findall(text)))))
        else:
            unit.append(f)
    return unit, db


def _pytest(args: list[str], env: dict[str, str], timeout: int) -> tuple[str, str]:
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfE", *args]
    try:
        res = subprocess.run(cmd, cwd=BACKEND, env=env, capture_output=True, text=True, timeout=timeout)
        out = res.stdout + res.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return f"TIMEOUT after {timeout}s", out
    lines = [l for l in out.splitlines() if re.match(r"^=* ?\d+ (passed|failed|error|skipped)|^=+ .*(passed|failed|error|skipped|no tests ran)", l)]
    summary = lines[-1].strip("= ") if lines else f"exit {res.returncode}, no summary"
    return summary, out


def run(server: str, files: list[str], redis_url: str | None, timeout: int, reuse_template: bool,
        log_dir: Path | None, keep: bool = False) -> int:
    check_server(server)
    if not reuse_template:
        build_template(server)
    paths = [BACKEND / f for f in files] if files else sorted(TESTS.glob("test_*.py"))
    unit, db = classify(paths)
    base_env = {k: v for k, v in os.environ.items() if k not in SEED_INPUT_VARS}
    base_env["REDIS_URL"] = DEAD_REDIS
    results: list[tuple[str, str]] = []
    # In every clone's name, so two runs against one server never share (or
    # drop) each other's databases. The template is shared: use --reuse-template
    # for a second concurrent run.
    run_id = secrets.token_hex(3)

    def record(label: str, summary: str, out: str) -> None:
        results.append((label, summary))
        print(f"{label:55s} {summary}", flush=True)
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"{label.replace('/', '_')}.log").write_text(out)

    if unit:
        # Unit suites get a clone too: a few import the app, which reads DATABASE_URL.
        name = f"unit_{run_id}_throwaway"
        env = {**base_env, **fresh_database(server, name)}
        record("unit suites", *_pytest([str(p.relative_to(BACKEND)) for p in unit], env, timeout))
        drop_database(server, name)
    for path, db_vars, redis_vars in db:
        name = f"{path.stem[5:][:40]}_{run_id}_throwaway"
        env = {**base_env, **fresh_database(server, name)}
        for var in db_vars:
            env[var] = env["DATABASE_URL"]
        if redis_vars and redis_url:
            env["REDIS_URL"] = redis_url
            for var in redis_vars:
                env[var] = redis_url
        record(str(path.relative_to(BACKEND)), *_pytest([str(path.relative_to(BACKEND))], env, timeout))
        if not keep:
            drop_database(server, name)

    bad = [(l, s) for l, s in results if re.search(r"failed|error|TIMEOUT|no summary|exit", s)]
    print(f"\n{len(results)} runs, {len(bad)} with failures")
    for label, summary in bad:
        print(f"  FAIL {label}: {summary}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build and verify the fresh-chain template")
    b.add_argument("--server", required=True)
    r = sub.add_parser("run", help="run suites, each on its own fresh clone")
    r.add_argument("--server", required=True)
    r.add_argument("--redis-url", help="a throwaway Redis, for suites that ask for one")
    r.add_argument("--timeout", type=int, default=900, help="per-suite timeout in seconds")
    r.add_argument("--reuse-template", action="store_true")
    r.add_argument("--log-dir", type=Path)
    r.add_argument("--keep-databases", action="store_true", help="leave each suite's clone for inspection")
    r.add_argument("files", nargs="*")
    a = p.parse_args(argv)
    if a.cmd == "build":
        build_template(a.server)
        return 0
    return run(a.server, a.files, a.redis_url, a.timeout, a.reuse_template, a.log_dir, a.keep_databases)


if __name__ == "__main__":
    sys.exit(main())
