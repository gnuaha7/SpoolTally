from __future__ import annotations

import re
import uuid
from datetime import UTC
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from spooltally.api.auth import Principal, require_full_access
from spooltally.db.models import CatalogRecord, CatalogRecordLink
from spooltally.db.session import get_session

router = APIRouter()


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


_DIGITS = re.compile(r"[0-9]+\Z", re.ASCII)
_KINDS = {"brand", "material", "filament", "variant", "size", "store", "purchase_link"}


def _page(value: str, minimum: int, maximum: int) -> int:
    if not _DIGITS.fullmatch(value):
        raise _error(422, "invalid_input", "Request validation failed.")
    significant = value.lstrip("0") or "0"
    if len(significant) > len(str(maximum)):
        raise _error(422, "invalid_input", "Request validation failed.")
    parsed = int(significant)
    if not minimum <= parsed <= maximum:
        raise _error(422, "invalid_input", "Request validation failed.")
    return parsed


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds" if value.microsecond else "seconds")
        .replace("+00:00", "Z")
    )


def _item(session: Session, row: CatalogRecord) -> dict[str, Any]:
    links = session.scalars(
        select(CatalogRecordLink)
        .where(CatalogRecordLink.source_record_id == row.id)
        .order_by(
            CatalogRecordLink.relationship_name.asc(), CatalogRecordLink.ordinal.asc()
        )
    ).all()
    return {
        "id": row.id,
        "kind": row.kind,
        "source_id": row.source_id,
        "identity_key": row.identity_key,
        "upstream_uuid": row.upstream_uuid,
        "parent_id": row.parent_id,
        "source_baseline_json": row.source_baseline_json,
        "typed_fields_json": row.typed_fields_json,
        "extension_json": row.extension_json,
        "raw_json": row.raw_json,
        "source_updated_at": _timestamp(row.source_updated_at),
        "first_seen_at": _timestamp(row.first_seen_at),
        "last_seen_at": _timestamp(row.last_seen_at),
        "links": [
            {
                "relationship_name": link.relationship_name,
                "ordinal": link.ordinal,
                "target_record_id": link.target_record_id,
            }
            for link in links
        ],
    }


@router.get("/catalog/records")
def list_records(
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
    kind: str | None = None,
    limit: Annotated[str, Query()] = "50",
    offset: Annotated[str, Query()] = "0",
) -> dict[str, Any]:
    del principal
    if kind is not None and kind not in _KINDS:
        raise _error(422, "invalid_input", "Request validation failed.")
    page_limit, page_offset = _page(limit, 1, 200), _page(offset, 0, 2**63 - 1)
    query = select(CatalogRecord)
    count = select(func.count()).select_from(CatalogRecord)
    if kind is not None:
        query, count = (
            query.where(CatalogRecord.kind == kind),
            count.where(CatalogRecord.kind == kind),
        )
    rows = session.scalars(
        query.order_by(CatalogRecord.id.asc()).limit(page_limit).offset(page_offset)
    ).all()
    return {
        "items": [_item(session, row) for row in rows],
        "limit": page_limit,
        "offset": page_offset,
        "total": session.scalar(count) or 0,
    }


@router.get("/catalog/records/{record_id}")
def get_record(
    record_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    del principal
    row = session.get(CatalogRecord, str(record_id))
    if row is None:
        raise _error(404, "not_found", "Resource not found.")
    return _item(session, row)
