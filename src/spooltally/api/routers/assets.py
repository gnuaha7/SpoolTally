from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.responses import Response

from spooltally.api.auth import Principal, require_full_access
from spooltally.db.models import Asset
from spooltally.db.session import get_session

router = APIRouter()


@router.get("/assets/{asset_id}")
def get_asset(
    asset_id: str,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_full_access)],
) -> Response:
    del principal
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(
            404, detail={"code": "not_found", "message": "Resource not found."}
        )
    if asset.media_type not in {"image/png", "image/jpeg"}:
        raise HTTPException(
            404, detail={"code": "not_found", "message": "Resource not found."}
        )
    return Response(
        content=asset.blob,
        media_type=asset.media_type,
        headers={"Content-Length": str(len(asset.blob))},
    )
