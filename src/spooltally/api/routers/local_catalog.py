from __future__ import annotations

import json
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NoReturn, overload

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from spooltally.api.auth import Principal, require_full_access
from spooltally.db.models import (
    CatalogRecord,
    InventoryState,
    LocalFilament,
    LocalManufacturer,
    SourceFieldOverride,
)
from spooltally.db.session import get_session
from spooltally.inventory.catalog import (
    apply_overrides,
    grams_to_mg,
    json_equal,
    mg_to_grams,
    parse_pointer,
    validate_new_pointer,
    validate_override_paths,
)
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


def _error(status: int, code: str = "invalid_input") -> HTTPException:
    message = "Resource not found." if status == 404 else "Request validation failed."
    return HTTPException(status, detail={"code": code, "message": message})


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _constant(value: str) -> NoReturn:
    raise ValueError(value)


def _finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _validate_json(value: Any) -> None:
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number")
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8")
            _validate_json(item)


async def _body(request: Request) -> Any:
    try:
        raw = (await request.body()).decode("utf-8")
        value = json.loads(
            raw,
            object_pairs_hook=_unique,
            parse_constant=_constant,
            parse_float=_finite,
        )
        _validate_json(value)
        return value
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise _error(422) from None


def _page(value: str, low: int, high: int) -> int:
    if not _DIGITS.fullmatch(value):
        raise _error(422)
    significant = value.lstrip("0") or "0"
    if len(significant) > len(str(high)):
        raise _error(422)
    number = int(significant)
    if not low <= number <= high:
        raise _error(422)
    return number


def _text(value: Any, maximum: int | None = None, *, trim: bool = False) -> str:
    if not isinstance(value, str):
        raise _error(422)
    result = value.strip() if trim else value
    if trim and not 1 <= len(result) <= (maximum or 0):
        raise _error(422)
    if not trim and maximum is not None and len(result) > maximum:
        raise _error(422)
    return result


@overload
def _uuid(value: Any, *, nullable: Literal[True]) -> str | None: ...


@overload
def _uuid(value: Any, *, nullable: Literal[False] = False) -> str: ...


def _uuid(value: Any, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise _error(422)
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise _error(422) from None


def _weight(value: Any) -> int:
    try:
        return grams_to_mg(value)
    except ValueError:
        raise _error(422) from None


def _time(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds" if value.microsecond else "seconds")
        .replace("+00:00", "Z")
    )


def _manufacturer(row: LocalManufacturer) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_brand_id": row.source_brand_id,
        "name": row.name,
        "notes": row.notes,
        "revision": row.revision,
        "created_at": _time(row.created_at),
        "updated_at": _time(row.updated_at),
    }


def _overrides(session: Session, row: LocalFilament) -> dict[str, Any]:
    return {
        item.json_pointer: item.value_json
        for item in session.scalars(
            select(SourceFieldOverride).where(
                SourceFieldOverride.local_filament_id == row.id
            )
        ).all()
    }


def _filament(session: Session, row: LocalFilament) -> dict[str, Any]:
    return {
        "id": row.id,
        "manufacturer_id": row.manufacturer_id,
        "source_filament_id": row.source_filament_id,
        "selected_variant_id": row.selected_variant_id,
        "name": row.name,
        "presentation_json": row.presentation_json,
        "notes": row.notes,
        "default_tare_g": mg_to_grams(row.default_tare_mg),
        "nominal_weight_g": mg_to_grams(row.nominal_weight_mg),
        "revision": row.revision,
        "created_at": _time(row.created_at),
        "updated_at": _time(row.updated_at),
        "overrides": _overrides(session, row),
    }


def _tag(session: Session, row: LocalManufacturer | LocalFilament, kind: str) -> str:
    state = session.get(InventoryState, 1)
    if state is None:
        raise RuntimeError("Inventory state is missing")
    return etag(state.dataset_generation, kind, row.id, row.revision)


def _json(
    body: object, status: int, headers: dict[str, str] | None = None
) -> WriteResult:
    return WriteResult(
        status,
        json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode(),
        {"Content-Type": "application/json", **(headers or {})},
    )


def _send(result: WriteResult) -> Response:
    return Response(result.body, status_code=result.status, headers=result.headers)


def _find(session: Session, model: type, identifier: str):
    row = session.get(model, identifier)
    if row is None:
        raise _error(404, "not_found")
    return row


def _source(
    session: Session, identifier: str | None, kind: str
) -> CatalogRecord | None:
    if identifier is None:
        return None
    row = session.get(CatalogRecord, identifier)
    if row is None or row.kind != kind:
        raise _error(422)
    return row


@router.get("/manufacturers")
def list_manufacturers(
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[str, Query()] = "50",
    offset: Annotated[str, Query()] = "0",
):
    del principal
    lim, off = _page(limit, 1, 200), _page(offset, 0, 2**63 - 1)
    rows = session.scalars(
        select(LocalManufacturer).order_by(LocalManufacturer.id).limit(lim).offset(off)
    ).all()
    return {
        "items": [_manufacturer(row) for row in rows],
        "limit": lim,
        "offset": off,
        "total": session.scalar(select(func.count()).select_from(LocalManufacturer))
        or 0,
    }


@router.get("/filaments")
def list_filaments(
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[str, Query()] = "50",
    offset: Annotated[str, Query()] = "0",
):
    del principal
    lim, off = _page(limit, 1, 200), _page(offset, 0, 2**63 - 1)
    rows = session.scalars(
        select(LocalFilament).order_by(LocalFilament.id).limit(lim).offset(off)
    ).all()
    return {
        "items": [_filament(session, row) for row in rows],
        "limit": lim,
        "offset": off,
        "total": session.scalar(select(func.count()).select_from(LocalFilament)) or 0,
    }


@router.post("/manufacturers")
async def create_manufacturer(
    request: Request,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
):
    body = await _body(request)
    if (
        not isinstance(body, dict)
        or set(body) - {"source_brand_id", "name", "notes"}
        or "name" not in body
    ):
        raise _error(422)
    name, notes = (
        _text(body["name"], 100, trim=True),
        _text(body.get("notes", "")),
    )
    source_id = _uuid(body.get("source_brand_id"), nullable=True)
    key = validate_idempotency_key(
        request.headers.get("Idempotency-Key"), required=True
    )
    digest = fingerprint(
        "POST",
        "/api/v1/manufacturers",
        {},
        {},
        None,
        {"source_brand_id": source_id, "name": name, "notes": notes},
    )
    with atomic_write(session):
        replay = get_replay(session, principal.token_id, key, digest)
        if replay:
            return _send(replay)
        _source(session, source_id, "brand")
        now, rid = datetime.now(UTC), str(uuid.uuid4())
        row = LocalManufacturer(
            id=rid,
            source_brand_id=source_id,
            name=name,
            notes=notes,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        result = _json(
            _manufacturer(row),
            201,
            {
                "ETag": _tag(session, row, "manufacturer"),
                "Location": f"/api/v1/manufacturers/{rid}",
            },
        )
        save_replay(session, principal.token_id, key, digest, result)
    return _send(result)


@router.post("/filaments")
async def create_filament(
    request: Request,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
):
    body = await _body(request)
    allowed = {
        "manufacturer_id",
        "source_filament_id",
        "selected_variant_id",
        "name",
        "presentation_json",
        "notes",
        "default_tare_g",
        "nominal_weight_g",
    }
    if (
        not isinstance(body, dict)
        or set(body) - allowed
        or "manufacturer_id" not in body
        or "name" not in body
    ):
        raise _error(422)
    mid, sid, vid = (
        _uuid(body["manufacturer_id"]),
        _uuid(body.get("source_filament_id"), nullable=True),
        _uuid(body.get("selected_variant_id"), nullable=True),
    )
    name, notes = (
        _text(body["name"], 120, trim=True),
        _text(body.get("notes", "")),
    )
    presentation = body.get("presentation_json", {})
    if not isinstance(presentation, dict):
        raise _error(422)
    tare = (
        None if body.get("default_tare_g") is None else _weight(body["default_tare_g"])
    )
    nominal = (
        None
        if body.get("nominal_weight_g") is None
        else _weight(body["nominal_weight_g"])
    )
    key = validate_idempotency_key(
        request.headers.get("Idempotency-Key"), required=True
    )
    canonical = {
        "manufacturer_id": mid,
        "source_filament_id": sid,
        "selected_variant_id": vid,
        "name": name,
        "notes": notes,
        "presentation_json": presentation,
        "default_tare_mg": tare,
        "nominal_weight_mg": nominal,
    }
    digest = fingerprint("POST", "/api/v1/filaments", {}, {}, None, canonical)
    with atomic_write(session):
        replay = get_replay(session, principal.token_id, key, digest)
        if replay:
            return _send(replay)
        _find(session, LocalManufacturer, mid)
        _source(session, sid, "filament")
        variant = _source(session, vid, "variant")
        if variant and (sid is None or variant.parent_id != sid):
            raise _error(422)
        now, rid = datetime.now(UTC), str(uuid.uuid4())
        row = LocalFilament(
            id=rid,
            manufacturer_id=mid,
            source_filament_id=sid,
            selected_variant_id=vid,
            name=name,
            presentation_json=presentation,
            notes=notes,
            default_tare_mg=tare,
            nominal_weight_mg=nominal,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        result = _json(
            _filament(session, row),
            201,
            {
                "ETag": _tag(session, row, "filament"),
                "Location": f"/api/v1/filaments/{rid}",
            },
        )
        save_replay(session, principal.token_id, key, digest, result)
    return _send(result)


def _if_match(request: Request) -> str:
    fields = request.headers.getlist("if-match")
    if len(fields) > 1:
        raise HTTPException(
            400,
            detail={
                "code": "invalid_precondition",
                "message": "If-Match must contain one tag.",
            },
        )
    return parse_if_match(fields[0] if fields else None)


@router.get("/manufacturers/{manufacturer_id}")
def get_manufacturer(
    manufacturer_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
):
    del principal
    row = _find(session, LocalManufacturer, str(manufacturer_id))
    return _send(
        _json(_manufacturer(row), 200, {"ETag": _tag(session, row, "manufacturer")})
    )


@router.get("/filaments/{filament_id}")
def get_filament(
    filament_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
):
    del principal
    row = _find(session, LocalFilament, str(filament_id))
    return _send(
        _json(_filament(session, row), 200, {"ETag": _tag(session, row, "filament")})
    )


@router.patch("/manufacturers/{manufacturer_id}")
async def patch_manufacturer(
    manufacturer_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
):
    body = await _body(request)
    fields = {"source_brand_id", "name", "notes"}
    if not isinstance(body, dict) or set(body) - fields:
        raise _error(422)
    clean = dict(body)
    if "name" in clean:
        clean["name"] = _text(clean["name"], 100, trim=True)
    if "notes" in clean:
        clean["notes"] = _text(clean["notes"])
    if "source_brand_id" in clean:
        clean["source_brand_id"] = _uuid(clean["source_brand_id"], nullable=True)
    accepted, key = (
        _if_match(request),
        validate_idempotency_key(request.headers.get("Idempotency-Key")),
    )
    mid = str(manufacturer_id)
    digest = fingerprint(
        "PATCH",
        "/api/v1/manufacturers/{manufacturer_id}",
        {"manufacturer_id": mid},
        {},
        accepted,
        clean,
    )
    with atomic_write(session):
        replay = get_replay(session, principal.token_id, key, digest)
        if replay:
            return _send(replay)
        row = _find(session, LocalManufacturer, mid)
        check_etag(accepted, _tag(session, row, "manufacturer"))
        if "source_brand_id" in clean:
            _source(session, clean["source_brand_id"], "brand")
        changed = any(getattr(row, field) != value for field, value in clean.items())
        if changed:
            for field, value in clean.items():
                setattr(row, field, value)
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            session.flush()
        result = _json(
            _manufacturer(row), 200, {"ETag": _tag(session, row, "manufacturer")}
        )
        save_replay(session, principal.token_id, key, digest, result)
    return _send(result)


@router.patch("/filaments/{filament_id}")
async def patch_filament(
    filament_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
):
    body = await _body(request)
    fields = {
        "manufacturer_id",
        "source_filament_id",
        "selected_variant_id",
        "name",
        "presentation_json",
        "notes",
        "default_tare_g",
        "nominal_weight_g",
    }
    if not isinstance(body, dict) or set(body) - fields:
        raise _error(422)
    clean = dict(body)
    if "manufacturer_id" in clean:
        clean["manufacturer_id"] = _uuid(clean["manufacturer_id"])
    if "source_filament_id" in clean:
        clean["source_filament_id"] = _uuid(clean["source_filament_id"], nullable=True)
    if "selected_variant_id" in clean:
        clean["selected_variant_id"] = _uuid(
            clean["selected_variant_id"], nullable=True
        )
    if "name" in clean:
        clean["name"] = _text(clean["name"], 120, trim=True)
    if "notes" in clean:
        clean["notes"] = _text(clean["notes"])
    if "presentation_json" in clean and not isinstance(
        clean["presentation_json"], dict
    ):
        raise _error(422)
    if "default_tare_g" in clean:
        clean["default_tare_mg"] = (
            None
            if clean.pop("default_tare_g") is None
            else _weight(body["default_tare_g"])
        )
    if "nominal_weight_g" in clean:
        clean["nominal_weight_mg"] = (
            None
            if clean.pop("nominal_weight_g") is None
            else _weight(body["nominal_weight_g"])
        )
    accepted, key = (
        _if_match(request),
        validate_idempotency_key(request.headers.get("Idempotency-Key")),
    )
    fid = str(filament_id)
    digest = fingerprint(
        "PATCH",
        "/api/v1/filaments/{filament_id}",
        {"filament_id": fid},
        {},
        accepted,
        clean,
    )
    with atomic_write(session):
        replay = get_replay(session, principal.token_id, key, digest)
        if replay:
            return _send(replay)
        row = _find(session, LocalFilament, fid)
        check_etag(accepted, _tag(session, row, "filament"))
        desired = {
            field: getattr(row, field)
            for field in fields
            if field not in {"default_tare_g", "nominal_weight_g"}
        }
        desired.update(
            {
                field: getattr(row, field)
                for field in ("default_tare_mg", "nominal_weight_mg")
            }
        )
        for field, value in clean.items():
            desired[field] = value
        _find(session, LocalManufacturer, desired["manufacturer_id"])
        _source(session, desired["source_filament_id"], "filament")
        variant = (
            _source(session, desired["selected_variant_id"], "variant")
            if desired["selected_variant_id"]
            else None
        )
        if variant and (
            desired["source_filament_id"] is None
            or variant.parent_id != desired["source_filament_id"]
        ):
            raise _error(422)
        changed = any(
            not json_equal(getattr(row, field), value) for field, value in clean.items()
        )
        if changed:
            for field, value in clean.items():
                setattr(row, field, value)
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            session.flush()
        result = _json(
            _filament(session, row), 200, {"ETag": _tag(session, row, "filament")}
        )
        save_replay(session, principal.token_id, key, digest, result)
    return _send(result)


@router.get("/filaments/{filament_id}/effective")
def get_effective(
    filament_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
):
    del principal
    row = _find(session, LocalFilament, str(filament_id))
    source = _source(session, row.source_filament_id, "filament")
    variant = (
        _source(session, row.selected_variant_id, "variant")
        if row.selected_variant_id
        else None
    )
    baseline = source.source_baseline_json if source else {}
    return {
        "filament_id": row.id,
        "source_filament_id": row.source_filament_id,
        "selected_variant_id": row.selected_variant_id,
        "source_baseline_json": baseline,
        "effective_source_json": apply_overrides(baseline, _overrides(session, row)),
        "selected_variant_source_json": variant.source_baseline_json
        if variant
        else None,
    }


@router.patch("/filaments/{filament_id}/overrides")
async def patch_overrides(
    filament_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_full_access)],
    session: Annotated[Session, Depends(get_session)],
):
    body = await _body(request)
    if not isinstance(body, dict) or set(body) - {"set", "reset"}:
        raise _error(422)
    sets, resets = body.get("set", {}), body.get("reset", [])
    if (
        not isinstance(sets, dict)
        or not isinstance(resets, list)
        or any(not isinstance(p, str) for p in resets)
    ):
        raise _error(422)
    if len(resets) != len(set(resets)) or set(sets).intersection(resets):
        raise _error(422)
    for path in [*sets, *resets]:
        try:
            parse_pointer(path)
        except ValueError:
            raise _error(422) from None
    accepted = _if_match(request)
    key = validate_idempotency_key(request.headers.get("Idempotency-Key"))
    fid = str(filament_id)
    digest = fingerprint(
        "PATCH",
        "/api/v1/filaments/{filament_id}/overrides",
        {"filament_id": fid},
        {},
        accepted,
        {"set": sets, "reset": resets},
    )
    with atomic_write(session):
        replay = get_replay(session, principal.token_id, key, digest)
        if replay:
            return _send(replay)
        row = _find(session, LocalFilament, fid)
        check_etag(accepted, _tag(session, row, "filament"))
        existing = {
            item.json_pointer: item
            for item in session.scalars(
                select(SourceFieldOverride).where(
                    SourceFieldOverride.local_filament_id == fid
                )
            ).all()
        }
        source = _source(session, row.source_filament_id, "filament")
        baseline = source.source_baseline_json if source else {}
        for path, value in sets.items():
            try:
                if path not in existing:
                    validate_new_pointer(baseline, path)
            except ValueError:
                raise _error(422) from None
            json.dumps(value, allow_nan=False)
        desired = set(existing) - set(resets) | set(sets)
        try:
            validate_override_paths([parse_pointer(path) for path in desired])
        except ValueError:
            raise _error(422) from None
        changed = any(
            path not in existing or not json_equal(existing[path].value_json, value)
            for path, value in sets.items()
        ) or any(path in existing for path in resets)
        if changed:
            for path in resets:
                if path in existing:
                    session.delete(existing[path])
            for path, value in sets.items():
                if path not in existing:
                    session.add(
                        SourceFieldOverride(
                            id=str(uuid.uuid4()),
                            local_filament_id=fid,
                            json_pointer=path,
                            value_json=value,
                            created_at=datetime.now(UTC),
                        )
                    )
                elif not json_equal(existing[path].value_json, value):
                    existing[path].value_json = value
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            session.flush()
        result = _json(
            _filament(session, row), 200, {"ETag": _tag(session, row, "filament")}
        )
        save_replay(session, principal.token_id, key, digest, result)
    return _send(result)
