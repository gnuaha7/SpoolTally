from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from spooltally.api.auth import Principal, require_full_access
from spooltally.db.models import CatalogRun, CatalogScheduleState
from spooltally.db.session import get_session
from spooltally.ofd.scheduler import (
    claim_manual_run,
    execute_run,
    router_lifespan,
)

router = APIRouter(lifespan=router_lifespan(None))
AUTH_DEPENDENCY = Depends(require_full_access)
SESSION_DEPENDENCY = Depends(get_session)


@router.post("/refresh", status_code=202)
async def refresh_catalog(
    request: Request,
    principal: Principal = AUTH_DEPENDENCY,
    session: Session = SESSION_DEPENDENCY,
):
    if not getattr(request.app.state, "refresh_owner", False):
        active = session.scalar(
            select(CatalogRun).where(CatalogRun.status == "running")
        )
        if active is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "refresh_unavailable",
                    "message": "Catalog refresh is unavailable.",
                },
            )
        return {"run_id": active.id}
    try:
        initialized = session.get(CatalogScheduleState, 1)
    except SQLAlchemyError:
        initialized = None
    if initialized is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "refresh_unavailable",
                "message": "Catalog refresh is unavailable.",
            },
        )
    now = request.app.state.clock()
    try:
        claim = claim_manual_run(request.app.state.session_factory, now)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "refresh_unavailable",
                "message": "Catalog refresh is unavailable.",
            },
        ) from exc
    if claim.should_execute:
        task = asyncio.create_task(
            asyncio.to_thread(
                execute_run,
                request.app.state.session_factory,
                claim.run_id,
                now=now,
                client_factory=getattr(request.app.state, "client_factory", None),
                stopped=getattr(request.app.state, "refresh_stop", None),
            )
        )
        request.app.state.refresh_worker = task
    return {"run_id": claim.run_id}
