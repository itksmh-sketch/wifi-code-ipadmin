"""Shared guards for endpoints that read a raw dict body.

Used where a field must be refused *by name* rather than silently dropped: the
body is parsed as a plain dict, checked against a positive allowlist, and only
then validated by a pydantic model. A positive allowlist means a column added
to the table later is refused by default instead of quietly becoming editable.
"""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import ValidationError


def reject_unknown_fields(payload: dict, allowed: set[str]) -> None:
    """400 on a non-object body, an empty body, or any field outside ``allowed``."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    extra = sorted(set(payload) - allowed)
    if extra:
        raise HTTPException(
            status_code=400,
            detail=f"These fields cannot be changed here: {', '.join(extra)}.",
        )
    if not payload:
        raise HTTPException(status_code=400, detail="Nothing to update.")


def parse_update(model, payload: dict):
    """Validate a hand-parsed payload, surfacing field errors as 400 rather than
    letting pydantic's ValidationError escape as a 500."""
    try:
        return model(**payload)
    except ValidationError as exc:
        messages = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise HTTPException(status_code=400, detail=messages or "Invalid request body.")
