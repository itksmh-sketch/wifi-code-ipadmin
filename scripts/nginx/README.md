# Host nginx sites (reference copies)

Copies of `/etc/nginx/sites-available/*` on the production VM, taken 2026-10-21.
nginx runs on the host, not in docker-compose; these files are **not** deployed
from here. Edit the host file (backup, `nginx -t`, reload), then refresh the copy.

| File | Serves |
|---|---|
| `default` | `ip-admin.duckdns.org`: HTTPS (Let's Encrypt, managed by Certbot) proxying to `127.0.0.1:8000`; port 80 redirects to HTTPS |
| `hotspot-portal` | `ip-admin-portal.duckdns.org`: HTTP only, `/portal/` and the CSP report endpoint; everything else 404 |
| `hotspot` | Catch-alls: port 80 returns 404; port 443 refuses TLS for any name not listed above (`ssl_reject_handshake`) |

The portal name stays plain HTTP because the router hotspot login page posts to
the router's own `http://` address, which browsers block from an HTTPS page.
