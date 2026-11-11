from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from spooltally.api.auth import Principal, require_full_access
from spooltally.db.models import InventoryState, Location, Spool
from spooltally.db.session import get_session
from spooltally.services.transactions import (
    WriteResult,
    atomic_write,
    check_etag,
    etag,
    fingerprint,
    get_replay,
    parse_if_match,
    save_replay,
    validate_idempotency_key,
)

router = APIRouter()
_DIGITS = re.compile(r"[0-9]+\Z", re.ASCII)


class LocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


async def _location_body(request: Request) -> LocationBody:
    try:
        raw = await request.body()
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        return LocationBody.model_validate(value)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError):
        raise _error(422, "invalid_input", "Request validation failed.") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite number {value}")


def _name(value: str) -> str:
    normalized = value.strip()
    if not 1 <= len(normalized) <= 100:
        raise _error(422, "invalid_input", "Request validation failed.")
    return normalized


def _page(value: str, minimum: int, maximum: int) -> int:
    if not _DIGITS.fullmatch(value):
        raise _error(422, "invalid_input", "Request validation failed.")
    significant_digits = value.lstrip("0") or "0"
    if len(significant_digits) > len(str(maximum)):
        raise _error(422, "invalid_input", "Request validation failed.")
    number = int(significant_digits)
    if not minimum <= number <= maximum:
        raise _error(422, "invalid_input", "Request validation failed.")
    return number


def _representation(location: Location) -> dict[str, str]:
    return {"id": location.id, "name": location.name}


def _etag(session: Session, location: Location) -> str:
    state = session.get(InventoryState, 1)
    if state is None:
        raise RuntimeError("Inventory state is missing")
    return etag(state.dataset_generation, "location", location.id, location.revision)


def _json_result(status: int, body: object, headers: dict[str, str]) -> WriteResult:
    return WriteResult(
        status=status,
        body=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json", **headers},
    )


def _send(result: WriteResult) -> Response:
    return Response(
        content=result.body, status_code=result.status, headers=result.headers
    )


def _find(session: Session, location_id: uuid.UUID) -> Location:
    location = session.get(Location, str(location_id))
    if location is None:
        raise _error(404, "not_found", "Resource not found.")
    return location


@router.get("/locations")
def list_locations(
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[str, Query()] = "50",
    offset: Annotated[str, Query()] = "0",
) -> dict[str, object]:
    del principal
    page_limit = _page(limit, 1, 200)
    page_offset = _page(offset, 0, 2**63 - 1)
    total = session.scalar(select(func.count()).select_from(Location)) or 0
    rows = session.scalars(
        select(Location)
        .order_by(Location.name.asc(), Location.id.asc())
        .limit(page_limit)
        .offset(page_offset)
    ).all()
    return {
        "items": [_representation(row) for row in rows],
        "limit": page_limit,
        "offset": page_offset,
        "total": total,
    }


@router.post("/locations")
def create_location(
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
    body: Annotated[LocationBody, Depends(_location_body)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    name = _name(body.name)
    key = validate_idempotency_key(idempotency_key, required=True)
    digest = fingerprint("POST", "/api/v1/locations", {}, {}, None, {"name": name})
    with atomic_write(session):
        replay = get_replay(session, principal.token_id, key, digest)
        if replay is not None:
            return _send(replay)
        resource_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        location = Location(
            id=resource_id, name=name, revision=1, created_at=now, updated_at=now
        )
        session.add(location)
        session.flush()
        result = _json_result(
            201,
            _representation(location),
            {
                "ETag": _etag(session, location),
                "Location": f"/api/v1/locations/{resource_id}",
            },
        )
        save_replay(session, principal.token_id, key, digest, result)
    return _send(result)


@router.get("/locations/{location_id}")
def get_location(
    location_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    del principal
    location = _find(session, location_id)
    return Response(
        content=json.dumps(
            _representation(location), ensure_ascii=False, separators=(",", ":")
        ).encode(),
        media_type="application/json",
        headers={"ETag": _etag(session, location)},
    )


@router.patch("/locations/{location_id}")
def update_location(
    location_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
    body: Annotated[LocationBody, Depends(_location_body)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    name = _name(body.name)
    accepted_tag = parse_if_match(if_match)
    key = validate_idempotency_key(idempotency_key)
    path = {"location_id": str(location_id)}
    digest = fingerprint(
        "PATCH",
        "/api/v1/locations/{location_id}",
        path,
        {},
        accepted_tag,
        {"name": name},
    )
    with atomic_write(session):
        replay = get_replay(session, principal.token_id, key, digest)
        if replay is not None:
            return _send(replay)
        location = _find(session, location_id)
        check_etag(accepted_tag, _etag(session, location))
        if location.name != name:
            location.name = name
            location.revision += 1
            location.updated_at = datetime.now(UTC)
            session.flush()
        result = _json_result(
            200, _representation(location), {"ETag": _etag(session, location)}
        )
        save_replay(session, principal.token_id, key, digest, result)
    return _send(result)


@router.delete("/locations/{location_id}")
def delete_location(
    location_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    accepted_tag = parse_if_match(if_match)
    key = validate_idempotency_key(idempotency_key)
    path = {"location_id": str(location_id)}
    digest = fingerprint(
        "DELETE", "/api/v1/locations/{location_id}", path, {}, accepted_tag, None
    )
    with atomic_write(session):
        replay = get_replay(session, principal.token_id, key, digest)
        if replay is not None:
            return _send(replay)
        location = _find(session, location_id)
        check_etag(accepted_tag, _etag(session, location))
        if session.scalar(
            select(Spool.id).where(Spool.location_id == location.id).limit(1)
        ):
            raise _error(409, "location_in_use", "Location is in use.")
        session.delete(location)
        result = WriteResult(status=204, body=b"", headers={})
        save_replay(session, principal.token_id, key, digest, result)
    return _send(result)
