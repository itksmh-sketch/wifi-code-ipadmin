"""Who is acting, and from where, for the request being handled.

Two context variables, filled in by two different things:

  * the request (ip, user agent, request id) by the ``audited`` dependency,
    which every audited route carries. It doubles as the marker the
    route-table guard test looks for, the same way require_active_operator
    marks suspension-gated routes;
  * the actor by the auth guards (get_authenticated_admin and
    get_authenticated_platform_owner) once a token has passed every check.

Both are only set from async dependencies, which FastAPI resolves in the same
task as the handler, so the handler sees them. Each request runs in its own
task, so nothing leaks from one request to the next. Code outside a request
(the worker, background tasks) passes the actor explicitly instead.
"""
from __future__ import annotations

import ipaddress
import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import Request

OPERATOR_USER = "operator_user"
PLATFORM_OWNER = "platform_owner"
SYSTEM = "system"

USER_AGENT_MAX = 256
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class Actor:
    actor_type: str
    actor_id: uuid.UUID | None = None
    label: str | None = None
    # The actor's own tenant (operator users only). A platform owner acting on
    # an operator names that operator per call instead.
    isp_operator_id: uuid.UUID | None = None

    @classmethod
    def for_admin(cls, admin) -> "Actor":
        return cls(OPERATOR_USER, admin.id, admin.email, admin.isp_operator_id)

    @classmethod
    def for_platform_owner(cls, owner) -> "Actor":
        return cls(PLATFORM_OWNER, owner.id, owner.email, None)

    @classmethod
    def system(cls) -> "Actor":
        return cls(SYSTEM)


@dataclass(frozen=True)
class RequestInfo:
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


_request_info: ContextVar[RequestInfo | None] = ContextVar("audit_request_info", default=None)
_actor: ContextVar[Actor | None] = ContextVar("audit_actor", default=None)


def _valid_ip(value: str | None) -> str | None:
    # The column is INET: anything that isn't an address (a test client's
    # "testclient", say) would fail the insert, and with it the action.
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def request_info_from(request: Request) -> RequestInfo:
    supplied = request.headers.get("x-request-id", "")
    return RequestInfo(
        ip=_valid_ip(request.client.host if request.client else None),
        user_agent=(request.headers.get("user-agent") or "")[:USER_AGENT_MAX] or None,
        request_id=supplied if _REQUEST_ID_RE.match(supplied) else uuid.uuid4().hex,
    )


async def audited(request: Request) -> None:
    """Route dependency: capture where the request came from. Also the marker
    tests/test_audit_guard.py requires on every audited route."""
    _request_info.set(request_info_from(request))


audited.is_audit_marker = True


def bind_actor(actor: Actor) -> None:
    _actor.set(actor)


def current_actor() -> Actor | None:
    return _actor.get()


def current_request_info() -> RequestInfo | None:
    return _request_info.get()
