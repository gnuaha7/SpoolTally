from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from spooltally.db.models import IdempotencyRecord

_IDEMPOTENCY_TTL = timedelta(seconds=604800)
_ETAG_RE = re.compile(r'^"st1\.([A-Za-z0-9_-]{43})"$')


@dataclass(frozen=True)
class WriteResult:
    status: int
    body: bytes
    headers: dict[str, str]


def _error(status: int, code: str, message: str, *, headers=None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message},
        headers=headers,
    )


def etag(generation: str, kind: str, resource_id: str, revision: int) -> str:
    try:
        generation_bytes = UUID(generation).bytes
        resource_bytes = UUID(resource_id).bytes
        if str(UUID(generation)) != generation or str(UUID(resource_id)) != resource_id:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise ValueError(
            "Generation and resource ID must be canonical UUIDs."
        ) from None
    if kind not in {"location", "manufacturer", "filament", "spool"}:
        raise ValueError("Unknown ETag resource kind.")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or not 1 <= revision < 2**63
    ):
        raise ValueError("Revision must be a positive signed 64-bit integer.")
    payload = (
        b"spooltally:etag:v1\0"
        + generation_bytes
        + b"\0"
        + kind.encode("utf-8")
        + b"\0"
        + resource_bytes
        + revision.to_bytes(8, "big")
    )
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f'"st1.{encoded.decode("ascii")}"'


def parse_if_match(value: str | None) -> str:
    if value is None:
        raise _error(428, "precondition_required", "If-Match is required.")
    match = _ETAG_RE.fullmatch(value)
    if match is None:
        raise _error(
            400, "invalid_precondition", "If-Match must contain one strong ETag."
        )
    try:
        decoded = base64.urlsafe_b64decode(match.group(1) + "=")
    except ValueError:
        decoded = b""
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(
        b"="
    ).decode() != match.group(1):
        raise _error(
            400, "invalid_precondition", "If-Match must contain one strong ETag."
        )
    return value


def check_etag(provided: str, current: str) -> None:
    if provided != current:
        raise _error(412, "precondition_failed", "The resource has changed.")


def validate_idempotency_key(
    value: str | None, *, required: bool = False
) -> str | None:
    valid = (
        value is not None
        and 1 <= len(value) <= 128
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )
    if valid:
        return value
    if value is None and not required:
        return None
    raise _error(
        422,
        "invalid_input",
        "Idempotency-Key must contain 1 to 128 visible ASCII characters.",
    )


def fingerprint(
    method: str,
    route: str,
    path_params: dict,
    query: dict,
    if_match: str | None,
    body: object,
) -> bytes:
    payload = {
        "method": method.upper(),
        "route": route,
        "path_params": path_params,
        "query": query,
        "if_match": if_match,
        "body": body,
    }
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("Fingerprint values must be finite JSON values.") from None
    return hashlib.sha256(serialized).digest()


def _is_database_busy(error: OperationalError) -> bool:
    original = error.orig
    if isinstance(original, sqlite3.OperationalError):
        code = getattr(original, "sqlite_errorcode", None)
        if code is not None and code & 0xFF in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            return True
        return "locked" in str(original).lower() or "busy" in str(original).lower()
    return False


@contextmanager
def atomic_write(session: Session) -> Iterator[None]:
    try:
        session.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
        yield
        session.commit()
    except OperationalError as error:
        session.rollback()
        if _is_database_busy(error):
            raise _error(
                503,
                "database_busy",
                "The database is busy. Retry the request.",
                headers={"Retry-After": "1"},
            ) from None
        raise
    except BaseException:
        session.rollback()
        raise


def _utc_now(now: datetime | None) -> datetime:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return moment.astimezone(UTC)


def get_replay(
    session: Session,
    token_id: str,
    key: str | None,
    digest: bytes,
    *,
    now: datetime | None = None,
) -> WriteResult | None:
    if key is None:
        return None
    moment = _utc_now(now)
    row = session.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.token_id == token_id,
            IdempotencyRecord.key == key,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if moment >= row.expires_at:
        session.delete(row)
        session.flush()
        return None
    if row.fingerprint != digest:
        raise _error(
            409,
            "idempotency_conflict",
            "Idempotency-Key was used with another request.",
        )
    return WriteResult(
        row.response_status,
        row.response_body.encode("utf-8"),
        dict(row.replay_headers_json),
    )


def save_replay(
    session: Session,
    token_id: str,
    key: str | None,
    digest: bytes,
    result: WriteResult,
    *,
    now: datetime | None = None,
) -> None:
    if key is None:
        return
    if not 200 <= result.status < 300:
        raise ValueError("Only successful 2xx responses can be saved for replay.")
    if len(digest) != 32:
        raise ValueError("Fingerprint must be a SHA-256 digest.")
    allowed_headers = {"content-type", "etag", "location"}
    headers = {
        name: value
        for name, value in result.headers.items()
        if name.lower() in allowed_headers
    }
    if result.status == 204 and (result.body or headers):
        raise ValueError(
            "204 replay results must have an empty body and no saved headers."
        )
    if result.status != 204 and result.body:
        try:
            decoded = result.body.decode("utf-8")
            json.loads(
                decoded,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ValueError("Replay response bodies must be JSON UTF-8.") from None
    moment = _utc_now(now)
    session.add(
        IdempotencyRecord(
            token_id=token_id,
            key=key,
            fingerprint=digest,
            response_status=result.status,
            response_body=result.body.decode("utf-8"),
            replay_headers_json=headers,
            created_at=moment,
            expires_at=moment + _IDEMPOTENCY_TTL,
        )
    )
