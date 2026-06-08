# ISP Hotspot Voucher & Billing System — Phase 1

Production-grade Wi-Fi hotspot automation platform for Ghana-based ISPs.
Handles centralized voucher authentication, plan management, multi-site topology, and RADIUS-based session accounting.

---

## Quick Start

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env with your values (especially JWT_SECRET and ENCRYPTION_KEY)

# 2. Start all services
docker-compose up -d

# 3. Run migrations (automatic on first boot)
docker-compose exec backend alembic upgrade head

# 4. Seed test data
RUN_SEED=true docker-compose up backend

# 5. Verify
curl http://localhost:8000/api/v1/health
```

## RADIUS High Availability (Primary + Secondary)

Configure MikroTik with two RADIUS servers:
- Primary: `freeradius` (UDP `1812` auth, `1813` accounting)
- Secondary: `freeradius-secondary` (UDP `1812` auth, `1813` accounting)

If the primary server times out, MikroTik will fail over to the secondary automatically.

## Architecture

```
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐
│  Admin Dashboard │────▶│  FastAPI API  │────▶│  PostgreSQL   │
│  (React+Vite)   │     │  (Python)     │     │               │
└─────────────────┘     └──────┬───────┘     └──────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   FreeRADIUS 3.x     │
                    │  (NAS authentication)│
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Captive Portal      │
                    │  (HTML/CSS/JS)       │
                    └─────────────────────┘
```

## Environment Variables

| Variable | Description | Required |
|---|---|---|
| `POSTGRES_DB` | Database name | Yes |
| `POSTGRES_USER` | Database user | Yes |
| `POSTGRES_PASSWORD` | Database password | Yes |
| `DATABASE_URL` | Async SQLAlchemy connection string | Yes |
| `SYNC_DATABASE_URL` | Sync connection string for Alembic | Yes |
| `JWT_SECRET` | HMAC secret for JWT signing | Yes |
| `JWT_EXPIRATION_MINUTES` | Token TTL | No (default: 60) |
| `RADIUS_COA_SECRET` | Shared secret for CoA/Disconnect to routers | Yes |
| `RADIUS_ACCOUNTING_SECRET` | Bearer token for RADIUS accounting endpoint | Yes |
| `ENCRYPTION_KEY` | Fernet key for encrypting NAS secrets at rest | Yes |
| `ADMIN_EMAIL` | Initial superadmin email (seed only) | No |
| `ADMIN_PASSWORD` | Initial superadmin password (seed only) | No |
| `RUN_SEED` | Run seed script on startup | No (default: false) |

## Adding a New Router

1. Create the router via API:
   ```bash
   curl -X POST http://localhost:8000/api/v1/sites/<site_id>/routers \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "MikroTik-CCR1009",
       "ip_address": "192.168.10.1",
       "nas_identifier": "router-town-site1",
       "nas_secret": "my_shared_secret",
       "is_active": true
     }'
   ```

2. The NAS secret is encrypted and stored in the `routers` table.

3. Add matching client entry in `freeradius/clients.conf`:
   ```
   client <nas_identifier> {
       ipaddr = <ip_address>
       secret = <nas_secret>
       shortname = <name>
   }
   ```

4. Reload FreeRADIUS: `docker-compose exec freeradius freeradius -C`

## Testing RADIUS Authentication

```bash
# Install radtest (Freeradius-utils on Ubuntu)
sudo apt install freeradius-utils

# Test with a seeded voucher (code format: XXXX-XXXX-XXXX-XXXX)
radtest <voucher_username> <voucher_password> localhost 0 <RADIUS_COA_SECRET>
```

## API Documentation

Once running, visit:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## FreeRADIUS SQL Queries

All RADIUS SQL is defined in `freeradius/sql.conf`. Key queries:

### Authorize
```sql
SELECT v.username AS username,
       v.password AS password,
       p.download_speed_kbps || 'k/' || p.upload_speed_kbps || 'k' AS Mikrotik-Rate-Limit,
       CASE WHEN v.expires_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM (v.expires_at - NOW()))::INTEGER
            ELSE 0
       END AS Session-Timeout,
       p.download_speed_kbps AS WISPr-Bandwidth-Max-Down,
       p.upload_speed_kbps AS WISPr-Bandwidth-Max-Up
FROM vouchers v
JOIN plans p ON v.plan_id = p.id
WHERE v.username = '%{SQL-User-Name}'
  AND v.status IN ('unused', 'active')
  AND (v.expires_at IS NULL OR v.expires_at > NOW())
  AND (v.data_used_mb < p.data_limit_mb OR p.data_limit_mb IS NULL)
  AND p.is_active = true;
```

### Accounting
```sql
-- Accounting-Start
INSERT INTO sessions (voucher_id, username, session_id, nas_ip, mac_address, ip_address,
                      started_at, upload_bytes, download_bytes, router_id)
SELECT v.id, '%{SQL-User-Name}', '%{Acct-Session-Id}', '%{NAS-IP-Address}',
       '%{Calling-Station-Id}'::macaddr, '%{Framed-IP-Address}'::inet,
       NOW(), 0, 0, r.id
FROM vouchers v
JOIN routers r ON r.nas_identifier = '%{NAS-Identifier}'
WHERE v.username = '%{SQL-User-Name}';

-- Accounting-Interim-Update / Stop
UPDATE sessions
SET stopped_at = CASE WHEN '%{Acct-Status-Type}' = 'Stop' THEN NOW() ELSE stopped_at END,
    terminate_cause = CASE WHEN '%{Acct-Status-Type}' = 'Stop' THEN '%{Acct-Terminate-Cause}' ELSE terminate_cause END,
    upload_bytes = COALESCE('%{Acct-Input-Octets}'::bigint, 0),
    download_bytes = COALESCE('%{Acct-Output-Octets}'::bigint, 0)
WHERE session_id = '%{Acct-Session-Id}';
```

## Phase 1 Scope

Included:
- Voucher CRUD and lifecycle management
- Plan management (time, data, hybrid)
- Multi-site / multi-town topology
- FreeRADIUS integration
- Session accounting
- Admin dashboard (React)
- Captive portal (plain HTML)
- Expiry cron job

Not included (future phases):
- Mobile money / payment integration
- Reseller / agent portal
- SMS / WhatsApp notifications
- Revenue analytics
- High availability / load balancing
- MAC address binding
- Subscriber self-service

## Security Notes

- All NAS secrets are AES-256 encrypted at rest (Fernet)
- Admin passwords are hashed with bcrypt
- RADIUS accounting endpoint uses a separate shared secret (not JWT)
- All database queries are parameterized — no string interpolation
- JWT tokens expire after configurable TTL

## Backup and Restore

Daily backups run at `02:00 UTC` via `supercronic` in the `backup` service and are written to the named Docker volume mounted at `/backups`.

Restore from a backup:

```bash
# copy dump out of backup volume if needed, then restore
gunzip -c hotspot_YYYYMMDD_HHMMSS.sql.gz | docker-compose exec -T postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

## Rate Limiting & DDoS

**Not implemented in Phase 1.** Plan for Phase 4:
- Add Cloudflare or NGINX rate limiting in front of the API
- Implement sliding-window rate limits per IP for the captive portal
- Configure MikroTik firewall rules for connection limiting

## Project Structure

```
/
├── docker-compose.yml
├── .env.example
├── README.md
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   └── src/
│       ├── app.py
│       ├── config.py
│       ├── db/
│       │   ├── base.py
│       │   ├── migrations/
│       │   └── seeds/
│       ├── modules/
│       │   ├── auth/
│       │   ├── towns/
│       │   ├── sites/
│       │   ├── routers/
│       │   ├── plans/
│       │   ├── vouchers/
│       │   └── sessions/
│       ├── radius/
│       ├── jobs/
│       └── middleware/
├── frontend/
│   └── (React + Vite admin dashboard)
├── portal/
│   └── (Captive portal static files)
└── freeradius/
    ├── radiusd.conf
    ├── sql.conf
    ├── clients.conf
    ├── dictionary
    └── sites-enabled/
```
