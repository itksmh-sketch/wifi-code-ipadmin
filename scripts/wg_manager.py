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
    GET    /health                                              -> {ok, interface, wg, wg_show_ok}

Security: binds to 127.0.0.1 only — never exposed to the internet. No auth
(only reachable from the host / docker host-network namespace).

Diagnostics: every request and every `wg` command (argv, return code, stdout,
stderr) is logged to stderr, and handler exceptions are logged with a full
traceback. So `docker compose logs wg-manager` shows exactly what happened.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WG_INTERFACE = os.environ.get("WG_INTERFACE", "wg0")
LISTEN_HOST = os.environ.get("WG_MANAGER_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("WG_MANAGER_PORT", "8999"))
HANDSHAKE_TIMEOUT = int(os.environ.get("WG_HANDSHAKE_TIMEOUT", "180"))
CMD_TIMEOUT = int(os.environ.get("WG_CMD_TIMEOUT", "15"))  # seconds per wg command
# Persisting peers to the .conf via `wg-quick save` is best-effort and the most
# fragile step in a container; disable with WG_PERSIST=0 if it misbehaves.
PERSIST = os.environ.get("WG_PERSIST", "1") not in ("0", "false", "False", "")
PERSISTENT_KEEPALIVE = "25"

# Use sudo only if not already root (e.g. running directly on the host as a user).
_SUDO = [] if os.geteuid() == 0 else ["sudo", "-n"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s wg-manager %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("wg-manager")


class WgCommandError(RuntimeError):
    """A `wg` command failed (non-zero exit, timeout, or binary missing)."""


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a wg/wg-quick command, logging argv + result. Honors CMD_TIMEOUT.

    With ``check=True`` (default) a non-zero exit raises :class:`WgCommandError`.
    Timeouts and a missing binary always raise (so a hung `wg-quick save`
    cannot make an HTTP request block forever).
    """
    argv = _SUDO + cmd
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        log.error("cmd TIMEOUT after %ss: %s", CMD_TIMEOUT, " ".join(argv))
        raise WgCommandError(f"command timed out after {CMD_TIMEOUT}s: {' '.join(cmd)}") from exc
    except FileNotFoundError as exc:
        log.error("cmd NOT FOUND: %s (%s)", argv[0] if argv else "?", exc)
        raise WgCommandError(f"binary not found: {exc}") from exc
    log.info(
        "cmd rc=%s argv=%s%s%s",
        result.returncode,
        " ".join(argv),
        f" | stdout={result.stdout.strip()!r}" if result.stdout.strip() else "",
        f" | stderr={result.stderr.strip()!r}" if result.stderr.strip() else "",
    )
    if check and result.returncode != 0:
        raise WgCommandError(f"{' '.join(cmd)} failed (rc={result.returncode}): {result.stderr.strip()}")
    return result


def add_peer(public_key: str, allowed_ip: str) -> None:
    _run([
        "wg", "set", WG_INTERFACE,
        "peer", public_key,
        "allowed-ips", allowed_ip,
        "persistent-keepalive", PERSISTENT_KEEPALIVE,
    ])
    _save_config()


def remove_peer(public_key: str) -> None:
    _run(["wg", "set", WG_INTERFACE, "peer", public_key, "remove"])
    _save_config()


def _save_config() -> None:
    """Persist runtime peers to /etc/wireguard/<iface>.conf so they survive a
    restart. Best-effort: never lets a save failure undo or mask the peer
    change that already took effect in the kernel."""
    if not PERSIST:
        return
    try:
        _run(["wg-quick", "save", WG_INTERFACE], check=False)
    except WgCommandError as exc:
        log.warning("persist (wg-quick save) skipped: %s", exc)


def _dump() -> list[list[str]]:
    """Return parsed lines of `wg show <iface> dump`. Raises if wg fails."""
    result = _run(["wg", "show", WG_INTERFACE, "dump"])
    return [ln.split("\t") for ln in result.stdout.splitlines() if ln.strip()]


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
    protocol_version = "HTTP/1.1"  # keep-alive friendly; we always set Content-Length

    def _read_body(self) -> bytes:
        """Read and return the full request body (draining the socket)."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.warning("client disconnected before response was sent")

    def log_message(self, fmt, *args):
        # Route the access log through our logger instead of stderr-printing.
        log.info("%s - %s", self.address_string(), fmt % args)

    # --- dispatch with a top-level guard so nothing escapes the thread ---

    def do_GET(self):
        self._guard(self._handle_get)

    def do_POST(self):
        self._guard(self._handle_post)

    def do_DELETE(self):
        self._guard(self._handle_delete)

    def _guard(self, fn):
        try:
            fn()
        except WgCommandError as exc:
            log.error("wg command error on %s %s: %s", self.command, self.path, exc)
            self._send(500, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.error("unhandled error on %s %s:\n%s", self.command, self.path, traceback.format_exc())
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _handle_get(self):
        path = urllib.parse.urlparse(self.path).path
        self._read_body()
        if path == "/health":
            wg_present = shutil.which("wg") is not None
            show_ok = False
            detail = None
            try:
                _dump()
                show_ok = True
            except WgCommandError as exc:
                detail = str(exc)
            self._send(200, {"ok": True, "interface": WG_INTERFACE, "wg": wg_present,
                             "wg_show_ok": show_ok, "detail": detail})
        elif path == "/peers":
            self._send(200, list_peers())
        elif path.startswith("/peer/") and path.endswith("/status"):
            pk = urllib.parse.unquote(path[len("/peer/"):-len("/status")])
            self._send(200, peer_status(pk))
        else:
            self._send(404, {"error": "not found"})

    def _handle_post(self):
        path = urllib.parse.urlparse(self.path).path
        raw = self._read_body()
        if path != "/peer":
            self._send(404, {"error": "not found"})
            return
        try:
            data = json.loads(raw or b"{}")
            public_key = data["public_key"]
            allowed_ip = data["allowed_ip"]
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"bad request: {exc}"})
            return
        add_peer(public_key, allowed_ip)
        self._send(200, {"ok": True, "public_key": public_key, "allowed_ip": allowed_ip})

    def _handle_delete(self):
        path = urllib.parse.urlparse(self.path).path
        self._read_body()
        if not path.startswith("/peer/"):
            self._send(404, {"error": "not found"})
            return
        pk = urllib.parse.unquote(path[len("/peer/"):])
        remove_peer(pk)
        self._send(200, {"ok": True, "removed": pk})


def _startup_selfcheck() -> None:
    log.info("starting: interface=%s sudo=%s persist=%s cmd_timeout=%ss",
             WG_INTERFACE, bool(_SUDO), PERSIST, CMD_TIMEOUT)
    if shutil.which("wg") is None:
        log.error("`wg` binary NOT on PATH — peer management will fail")
        return
    try:
        _run(["wg", "show", WG_INTERFACE, "public-key"])
        log.info("self-check OK: `wg show %s` works", WG_INTERFACE)
    except WgCommandError as exc:
        log.error("self-check FAILED: cannot read interface %s: %s", WG_INTERFACE, exc)


def main():
    _startup_selfcheck()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log.info("wg-manager listening on %s:%s, interface=%s", LISTEN_HOST, LISTEN_PORT, WG_INTERFACE)
    server.serve_forever()


if __name__ == "__main__":
    main()
