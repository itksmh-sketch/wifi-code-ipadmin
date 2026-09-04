# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Multi-tenant SaaS ISP hotspot voucher & billing platform for the Ghanaian market.
ISP operators manage MikroTik hotspot routers, sell internet vouchers, and take
mobile-money/card payments.

## Running environment / safety

- This repo may be operated on the **production VM** (`hotspot-server`, GCP `34.122.11.114`).
  It holds live operator data, secrets, and a production Postgres DB.
- **Never** edit, commit, or print the contents of `.env`. It is environment-specific and is
  never committed. Each environment maintains its own.
- **Never** touch encryption keys or secrets: `ENCRYPTION_KEY`, `PORTAL_TOKEN_SECRET`, the
  three JWT secrets, `nas_secret`/`nas_secret_plain` values.
- Treat as **report-before-applying** (propose, wait for explicit go-ahead): any change to
  `docker-compose.yml`, FreeRADIUS configs, firewall/iptables rules, the database beyond
  SELECTs, or anything that restarts a running service. A wrong iptables rule on this remote
  VM can sever SSH access.
- Read, diagnose, run SELECTs, read logs, and inspect freely.

## Stack

- **Backend**: Python / FastAPI, running **in the `hotspot-backend` container** with
  `network_mode: host`. Host networking means uvicorn shows up in host `ps` and *looks*
  bare-metal — it is not. `docker compose logs backend` works and streams request logs.
  `./backend` is bind-mounted to `/app` and uvicorn runs `--reload`, so **any edit under
  `backend/` hot-reloads the live API within seconds** — on the production VM, treat a
  backend edit as immediately live. The host has no `fastapi` installed; run backend Python
  via `docker exec hotspot-backend python ...`.
- **DB**: PostgreSQL (container, host port 5432) behind PgBouncer (host port 6432). Backend
  connects via PgBouncer (6432); FreeRADIUS connects direct to Postgres (5432).
- **FreeRADIUS** 3.2.6: primary + secondary (HA). Dynamic SQL clients loaded from `routers`
  table.
- **Workers**: Redis/arq (containerized).
- **Frontend** (two stacks — know which one you are editing):
  - **React/Vite SPA**, built to `backend/static/admin/`, served at `/admin/` — this is the
    **operator-admin dashboard** and is canonical for it.
  - **Vanilla HTML** (server-served, no build step) for the **captive portal**, the
    **reseller portal**, and the **platform-owner portal**
    (`backend/src/platform_portal/`, served at `/platform/*`).
  - The platform-owner portal is **vanilla, not React** — it lives only in
    `backend/src/platform_portal/`. A parallel React platform portal once existed at
    `frontend/src/pages/platform/*.jsx` (reachable under `/admin/platform/*`); it was
    **retired** and those pages are deleted. `/admin/platform/*` no longer resolves to
    anything — it falls through to the operator-admin login/dashboard. Platform-owner
    features live in the vanilla portal only; do not recreate React platform pages.
    (`platformApiCall` in `App.jsx` survives with no callers — see the note there.)
- **Dev workflow**: local Windows `C:\xampp\htdocs\wifi-code Project` → GitHub
  `itksmh-sketch/wifi-code-ipadmin` → server pulls and restarts.

## Common Commands

### Start / Stop
```bash
docker-compose up -d                          # start all services, backend included
docker-compose down
docker compose logs -f backend                # backend request logs (NOT empty)
docker-compose logs -f worker
```

### Database Migrations (run in the container — the host has no Python env for this)
```bash
# `alembic upgrade head` also runs automatically in the backend container's start command.
docker exec hotspot-backend alembic upgrade head
docker exec hotspot-backend alembic revision --autogenerate -m "description"
```

### Seed Data
```bash
RUN_SEED=true python -m src.db.seeds.seed
```

### Tests (integration — require a running server)
```bash
cd backend
TEST_BASE_URL=http://localhost:8000 python -m pytest tests/ -v
TEST_BASE_URL=http://localhost:8000 python -m pytest tests/test_billing.py -v
```
Tests use plain `urllib` and hit a live server. They fail/skip in sandboxes without one — note this rather than treating it as a failure.

### Frontend
```bash
cd frontend && npm run dev      # dev server on :3000, proxies /api → :8000
cd frontend && npm run build    # build → backend/static/admin/ (served at /admin/)
```
`vite build` must be clean before shipping any frontend change.

### FreeRADIUS
```bash
docker-compose exec freeradius freeradius -C   # validate config
# For live RADIUS verification use freeradius -X foreground with real overrides loaded.
# SIGHUP does NOT reload SQL read_clients — a router add/change or client_query change
# requires a full restart of the primary container, not a HUP.
```

## Architecture (conceptual model)

- **Web app (FastAPI)** = business logic / billing brain.
- **RADIUS (FreeRADIUS)** = auth/accounting gatekeeper.
- **MikroTik routers** = network enforcer.

## VPN-only invariant (IMPORTANT — recurring source of bugs)

The platform is **VPN-only**: routers reach the server only over the WireGuard tunnel
(`10.100.0.0/24`, server tunnel IP `10.100.0.1`). A router's `ip_address` column is a
**display-only label**; all real connectivity (RouterOS API, RADIUS, CoA/disconnect) must
target `wg_tunnel_ip`, falling back to `ip_address` only for legacy non-tunneled routers,
and erroring if neither exists. When adding any router-facing code path, follow this rule —
several past bugs came from a path still using `ip_address`/`radius_public_host` instead of
the tunnel IP.

Known routers: Osu (`wg_tunnel_ip 10.100.0.2`), Aflao-net (`10.100.0.3`),
EastLegon (no tunnel — seed leftover).

## Multi-tenancy

Three JWT issuers with separate secrets:
- `"platform_owner"` → `PLATFORM_OWNER_JWT_SECRET`
- `"admin"` → `JWT_SECRET`
- `"reseller"` → `RESELLER_JWT_SECRET`

All tenant-scoped tables have `isp_operator_id NOT NULL`; enforce tenant isolation on every
operator-scoped query. `TenantContext` dataclass (`backend/src/middleware/auth.py`) threads
this through all request handlers.

Route namespacing: `/platform/*`, `/admin`, `/portal` (captive), `/reseller`, `/apply`.
Paystack webhooks are slug-scoped: `/api/v1/webhooks/paystack/{operator_slug}`.

## RADIUS specifics

- Dynamic clients via `client_query` in `sql.conf`: schema must be `id, nasname, shortname,
  type, secret` (+ optional `server`) in that order; `nasname` keyed on `wg_tunnel_ip`.
- `nas_secret_plain` = plaintext for FreeRADIUS; `nas_secret` = AES-256 (Fernet) encrypted
  for the app layer.
- Primary FreeRADIUS is host-networked (sees true `10.100.0.x` source IPs); under host
  networking it reaches Postgres at `127.0.0.1:5432` (no Docker DNS), and binds `0.0.0.0` —
  so RADIUS ports MUST be firewalled to `wg0`/`10.100.0.0/24` only.
- CoA/Disconnect: target `User-Name + Framed-IP-Address` (not `Acct-Session-Id`); expect
  response codes 41/42 (Disconnect-ACK/NAK).

## Voucher lifecycle

`unused → active → exhausted / expired / disabled`

`expires_at` is set once at first activation (Accounting-Start). `Session-Timeout` in the
authorize query uses `EXTRACT(EPOCH FROM (expires_at - NOW()))` so countdowns persist across
reconnects.

## Frontend / API conventions

- Admin pages use `apiCall` / `platformApiCall` (defined in `App.jsx`). These throw `ApiError`
  on any non-2xx (carries `.status`/`.body`); wrap calls in try/catch and surface errors to
  the user.
- Public portal pages (`portal/`, `reseller/`) use plain `fetch`, not `apiCall`.
- Captive portal redirect uses a signed `rt` token (purpose `portal_redirect`, no expiry,
  signed with `PORTAL_TOKEN_SECRET`) carried through login → plans → pay → success.
  Operator/branding resolves from `rt`, falling back to gateway-IP match then platform
  defaults.

## Key files

- `backend/src/app.py` — all routers registered here; also serves the React SPA
- `backend/src/db/models.py` — single file with all SQLAlchemy models
- `backend/src/config.py` — all `Settings` fields (pydantic-settings, reads `.env`)
- `backend/src/middleware/auth.py` — `TenantContext`, JWT decode, role/operator guards
- `freeradius/sql.conf` — all RADIUS SQL (authorize, accounting start/stop/interim, client query)
- `freeradius/clients.conf` — static NAS entries (dynamic ones come from `client_query`)
- `scripts/wg_manager.py` — WireGuard sidecar (binds `127.0.0.1:8999`)
- `backend/src/jobs/worker.py` — all arq cron job registrations
- `backend/src/modules/payments/providers/base.py` — `PaymentProvider` ABC all providers implement
