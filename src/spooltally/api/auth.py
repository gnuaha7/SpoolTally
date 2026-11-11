from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from spooltally.db.models import TokenRecord
from spooltally.db.session import get_session


@dataclass(frozen=True)
class Principal:
    token_id: str
    scopes: tuple[str, ...]


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "unauthorized", "message": "Authentication is required."},
    )


def _token_digest(credential: str | None) -> bytes:
    if credential is None:
        raise _unauthorized()
    scheme, separator, encoded = credential.partition(" ")
    if scheme.lower() != "bearer" or not separator or len(encoded) != 43:
        raise _unauthorized()
    try:
        raw = encoded.encode("ascii")
        decoded = base64.b64decode(raw + b"=", altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error):
        raise _unauthorized() from None
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(b"=") != raw:
        raise _unauthorized()
    return hashlib.sha256(decoded).digest()


def authenticated_principal(
    session: Annotated[Session, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    digest = _token_digest(authorization)
    row = session.execute(
        select(TokenRecord).where(
            TokenRecord.token_hash == digest, TokenRecord.revoked_at.is_(None)
        )
    ).scalar_one_or_none()
    if row is None:
        session.commit()
        raise _unauthorized()
    principal = Principal(row.id, tuple(sorted(scope.scope for scope in row.scopes)))
    session.commit()
    if "full_access" not in principal.scopes:
        raise _unauthorized()
    return principal


def require_full_access(
    principal: Annotated[Principal, Depends(authenticated_principal)],
) -> Principal:
    if "full_access" not in principal.scopes:
        raise _unauthorized()
    return principal
