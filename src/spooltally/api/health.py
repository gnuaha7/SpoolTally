from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter()


@router.get("/healthz")
def health(request: Request) -> JSONResponse:
    engine = request.app.state.engine
    if not request.app.state.settings.database_path.is_file():
        return JSONResponse({"status": "not_ready"}, status_code=503)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ready"})
