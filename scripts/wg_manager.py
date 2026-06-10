#!/usr/bin/env python3
"""wg-manager — minimal privileged sidecar for WireGuard peer management.

Runs on the host network (see docker-compose) so it can manage the real
``wg0`` interface. Exposes a tiny HTTP API on 127.0.0.1:8999 that the backend
calls. Uses only the Python standard library so it runs in a bare
``python:3.11-slim`` image with no pip install.

Endpoints:
    POST   /peer                      {public_key, allowed_ip}  -> add/update peer
    DELETE /peer/{public_key}                                   -> remove peer
    GET    /peer/{public_key}/status                            -> handshake + transfer
    GET    /peers                                               -> all peers w/ status
    GET    /health                                              -> {ok, interface, wg}

Security: binds to 127.0.0.1 only — never exposed to the internet. No auth
(only reachable from the host / docker host-network namespace).
"""
import json
import os
import shutil
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WG_INTERFACE = os.environ.get("WG_INTERFACE", "wg0")
LISTEN_HOST = os.environ.get("WG_MANAGER_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("WG_MANAGER_PORT", "8999"))
HANDSHAKE_TIMEOUT = int(os.environ.get("WG_HANDSHAKE_TIMEOUT", "180"))
PERSISTENT_KEEPALIVE = "25"

# Use sudo only if not already root (e.g. running directly on the host as a user).
_SUDO = [] if os.geteuid() == 0 else ["sudo"]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(_SUDO + cmd, capture_output=True, text=True)


def add_peer(public_key: str, allowed_ip: str) -> None:
    result = _run([
        "wg", "set", WG_INTERFACE,
        "peer", public_key,
        "allowed-ips", allowed_ip,
        "persistent-keepalive", PERSISTENT_KEEPALIVE,
    ])
    if result.returncode != 0:
        raise RuntimeError(f"wg set peer failed: {result.stderr.strip()}")
    _save_config()


def remove_peer(public_key: str) -> None:
    result = _run(["wg", "set", WG_INTERFACE, "peer", public_key, "remove"])
    if result.returncode != 0:
        raise RuntimeError(f"wg remove peer failed: {result.stderr.strip()}")
    _save_config()


def _save_config() -> None:
    # Persist current runtime peers to /etc/wireguard/<iface>.conf so they
    # survive a restart. Best-effort — runtime state is the source of truth.
    _run(["wg-quick", "save", WG_INTERFACE])


def _dump() -> list[list[str]]:
    """Return parsed lines of `wg show <iface> dump`. Raises if wg fails."""
    result = _run(["wg", "show", WG_INTERFACE, "dump"])
    if result.returncode != 0:
        raise RuntimeError(f"wg show failed: {result.stderr.strip()}")
    lines = [ln.split("\t") for ln in result.stdout.splitlines() if ln.strip()]
    return lines


def _peer_record(parts: list[str]) -> dict:
    # peer dump line: public_key preshared endpoint allowed_ips
    #                 latest_handshake transfer_rx transfer_tx persistent_keepalive
    last_handshake = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() and parts[4] != "0" else None
    rx = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
    tx = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0
    connected = last_handshake is not None and (time.time() - last_handshake) < HANDSHAKE_TIMEOUT
    return {
        "public_key": parts[0],
        "allowed_ips": parts[3] if len(parts) > 3 else "",
        "last_handshake": last_handshake,
        "transfer_rx": rx,
        "transfer_tx": tx,
        "connected": connected,
    }


def peer_status(public_key: str) -> dict:
    for parts in _dump()[1:]:  # skip line 0 (the interface itself)
        if parts and parts[0] == public_key:
            return _peer_record(parts)
    return {"public_key": public_key, "connected": False, "last_handshake": None,
            "transfer_rx": 0, "transfer_tx": 0, "allowed_ips": ""}


def list_peers() -> list[dict]:
    return [_peer_record(parts) for parts in _dump()[1:] if parts and parts[0]]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict | list) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter logging
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/health":
                wg_present = shutil.which("wg") is not None
                self._send(200, {"ok": True, "interface": WG_INTERFACE, "wg": wg_present})
            elif path == "/peers":
                self._send(200, list_peers())
            elif path.startswith("/peer/") and path.endswith("/status"):
                pk = urllib.parse.unquote(path[len("/peer/"):-len("/status")])
                self._send(200, peer_status(pk))
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/peer":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            public_key = data["public_key"]
            allowed_ip = data["allowed_ip"]
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"bad request: {exc}"})
            return
        try:
            add_peer(public_key, allowed_ip)
            self._send(200, {"ok": True, "public_key": public_key, "allowed_ip": allowed_ip})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/peer/"):
            self._send(404, {"error": "not found"})
            return
        pk = urllib.parse.unquote(path[len("/peer/"):])
        try:
            remove_peer(pk)
            self._send(200, {"ok": True, "removed": pk})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"wg-manager listening on {LISTEN_HOST}:{LISTEN_PORT}, interface={WG_INTERFACE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
