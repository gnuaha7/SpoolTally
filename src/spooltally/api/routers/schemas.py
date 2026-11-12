from __future__ import annotations

from datetime import UTC
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from spooltally.api.auth import Principal, require_full_access
from spooltally.db.models import CatalogSchema
from spooltally.db.session import get_session

router = APIRouter()


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _item(row: CatalogSchema) -> dict[str, Any]:
    fetched = (
        row.fetched_at.astimezone(UTC)
        .isoformat(timespec="microseconds" if row.fetched_at.microsecond else "seconds")
        .replace("+00:00", "Z")
    )
    return {
        "schema_name": row.schema_name,
        "source_version": row.source_version,
        "fetched_at": fetched,
        "source_json": row.source_json,
    }


@router.get("/catalog/schemas")
def list_schemas(
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    del principal
    rows = session.scalars(
        select(CatalogSchema).order_by(CatalogSchema.schema_name.asc())
    ).all()
    return {"items": [_item(row) for row in rows]}


@router.get("/catalog/schemas/{schema_name}")
def get_schema(
    schema_name: str,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    del principal
    row = session.get(CatalogSchema, schema_name)
    if row is None:
        raise _error(404, "not_found", "Resource not found.")
    return _item(row)
