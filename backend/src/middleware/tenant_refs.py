"""Loading a row a request names by id, scoped to the caller's operator.

Any id a client sends (path, query or body) that the server then stores or
acts on has to belong to the caller's operator. load_owned() is the one way
to check that: the row, or a 404 that reads the same whether the id doesn't
exist, isn't a valid id, or belongs to another operator, so a probe can't
tell those apart.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession


async def load_owned(db: AsyncSession, model, row_id: Any, operator_id: uuid.UUID, *, label: str,
                     allow_global: bool = False):
    """The ``model`` row with ``row_id`` owned by ``operator_id``, or 404.

    ``allow_global`` also accepts a platform-wide row (isp_operator_id NULL),
    as config templates have.
    """
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")
    try:
        row_uuid = row_id if isinstance(row_id, uuid.UUID) else uuid.UUID(str(row_id))
    except (TypeError, ValueError):
        raise not_found
    owner = model.isp_operator_id == operator_id
    if allow_global:
        owner = or_(owner, model.isp_operator_id.is_(None))
    row = (await db.execute(select(model).where(model.id == row_uuid, owner))).scalar_one_or_none()
    if row is None:
        raise not_found
    return row
