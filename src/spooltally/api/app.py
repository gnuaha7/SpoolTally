from collections.abc import Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from spooltally.api.health import router as health_router
from spooltally.api.registry import register_routers
from spooltally.config import Settings, load_settings
from spooltally.db.session import create_engine, create_session_factory


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    selected_settings = settings or load_settings()
    engine = create_engine(selected_settings.database_path)

    app = FastAPI(lifespan=lifespan)
    app.state.settings = selected_settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.include_router(health_router)
    register_routers(app)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if (
            isinstance(detail, Mapping)
            and isinstance(detail.get("code"), str)
            and isinstance(detail.get("message"), str)
        ):
            code, message = detail["code"], detail["message"]
        elif exc.status_code == 404:
            code, message = "not_found", "Resource not found."
        elif exc.status_code == 405:
            code, message = "method_not_allowed", "Method not allowed."
        else:
            code, message = "request_failed", "Request failed."
        headers = {
            key: value
            for key, value in (exc.headers or {}).items()
            if key.lower() not in {"content-type", "etag", "location"}
        }
        return JSONResponse(
            {"error": {"code": code, "message": message}},
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_input",
                    "message": "Request validation failed.",
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "internal_error", "message": "Internal server error."}},
            status_code=500,
        )

    return app
