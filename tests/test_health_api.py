from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from spooltally.api.app import create_app
from spooltally.config import Settings
from spooltally.db.session import get_session

SESSION_DEPENDENCY = Depends(get_session)


def test_health_is_ready_for_initialized_schema_pending_database(
    tmp_path: Path,
) -> None:
    """REQ-CORE-001: readiness works before schema migrations exist."""
    database_path = tmp_path / "spooltally.sqlite3"
    database_path.touch()
    app = create_app(Settings(tmp_path, "127.0.0.1", 8000))

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    with app.state.engine.connect() as connection:
        tables = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).all()
    assert tables == []


def test_health_is_not_ready_when_database_file_is_missing(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, "127.0.0.1", 8000))

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_framework_not_found_uses_safe_error_envelope(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, "127.0.0.1", 8000))
    with TestClient(app, raise_server_exceptions=False) as client:
        missing = client.get("/not-a-route")
        metrics = client.get("/metrics")

    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "not_found", "message": "Resource not found."}
    }
    assert metrics.status_code == 404


def test_framework_method_not_allowed_uses_safe_error_envelope_and_allow_header(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(tmp_path, "127.0.0.1", 8000))
    with TestClient(app, raise_server_exceptions=False) as client:
        wrong_method = client.post("/healthz")

    assert wrong_method.status_code == 405
    assert wrong_method.json() == {
        "error": {"code": "method_not_allowed", "message": "Method not allowed."}
    }
    assert wrong_method.headers["allow"] == "GET"


def test_http_exception_uses_shared_error_envelope(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, "127.0.0.1", 8000))

    @app.get("/test/http-error")
    def http_error() -> None:
        raise HTTPException(
            status_code=503,
            detail={"code": "database_busy", "message": "Database is busy."},
            headers={
                "Retry-After": "1",
                "ETag": "secret",
                "Location": "/private",
                "Content-Type": "text/plain",
            },
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        busy = client.get("/test/http-error")

    assert busy.status_code == 503
    assert busy.json() == {
        "error": {"code": "database_busy", "message": "Database is busy."}
    }
    assert busy.headers["retry-after"] == "1"
    assert busy.headers["content-type"] == "application/json"
    assert "etag" not in busy.headers
    assert "location" not in busy.headers


def test_request_validation_uses_shared_error_envelope(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, "127.0.0.1", 8000))

    @app.get("/test/validation/{value}")
    def validation(value: int) -> int:
        return value

    with TestClient(app, raise_server_exceptions=False) as client:
        invalid = client.get("/test/validation/not-an-integer")

    assert invalid.status_code == 422
    assert invalid.json() == {
        "error": {"code": "invalid_input", "message": "Request validation failed."}
    }
    assert "not-an-integer" not in invalid.text


def test_unhandled_exception_uses_safe_error_envelope(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, "127.0.0.1", 8000))

    @app.get("/test/unhandled")
    def unhandled() -> None:
        raise RuntimeError("/private/token hash sql secret")

    with TestClient(app, raise_server_exceptions=False) as client:
        unexpected = client.get("/test/unhandled")

    assert unexpected.status_code == 500
    assert unexpected.json() == {
        "error": {"code": "internal_error", "message": "Internal server error."}
    }
    assert "/private/token hash sql secret" not in unexpected.text


def test_http_exception_string_detail_uses_safe_fallback(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, "127.0.0.1", 8000))

    @app.get("/test/string-detail")
    def string_detail() -> None:
        raise HTTPException(status_code=418, detail="do not echo this")

    with TestClient(app, raise_server_exceptions=False) as client:
        unrecognized = client.get("/test/string-detail")

    assert unrecognized.status_code == 418
    assert unrecognized.json() == {
        "error": {"code": "request_failed", "message": "Request failed."}
    }


def test_request_session_is_closed_after_response(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, "127.0.0.1", 8000))
    sessions: list[Session] = []

    def session_factory() -> Session:
        session = Session(bind=app.state.engine)
        sessions.append(session)
        return session

    app.state.session_factory = session_factory

    @app.get("/test/session")
    def session_endpoint(session: Session = SESSION_DEPENDENCY) -> dict[str, int]:
        return {"result": session.execute(text("SELECT 1")).scalar_one()}

    with TestClient(app) as client:
        first = client.get("/test/session")
        assert app.state.engine.pool.checkedout() == 0
        second = client.get("/test/session")
        assert app.state.engine.pool.checkedout() == 0

    assert first.status_code == 200
    assert first.json() == {"result": 1}
    assert second.status_code == 200
    assert second.json() == {"result": 1}
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]


def test_sqlite_transaction_event_uses_validated_mode(tmp_path: Path) -> None:
    from spooltally.db.session import create_engine

    engine = create_engine(tmp_path / "spooltally.sqlite3")
    with engine.connect() as first, engine.connect() as second:
        deferred_sql: list[str] = []
        immediate_sql: list[str] = []
        first.connection.driver_connection.set_trace_callback(deferred_sql.append)
        second.connection.driver_connection.set_trace_callback(immediate_sql.append)
        assert first.exec_driver_sql("SELECT 1").scalar_one() == 1
        assert (
            second.execution_options(sqlite_begin_mode="IMMEDIATE")
            .exec_driver_sql("SELECT 1")
            .scalar_one()
            == 1
        )

    assert deferred_sql == ["BEGIN DEFERRED", "SELECT 1", "ROLLBACK"]
    assert immediate_sql == ["BEGIN IMMEDIATE", "SELECT 1", "ROLLBACK"]
    with engine.connect() as connection:
        invalid_sql: list[str] = []
        connection.connection.driver_connection.set_trace_callback(invalid_sql.append)
        try:
            connection.execution_options(sqlite_begin_mode="EXCLUSIVE").exec_driver_sql(
                "SELECT 1"
            )
        except ValueError as exc:
            assert str(exc) == "Invalid SQLite transaction mode."
        else:
            raise AssertionError("accepted unsupported SQLite begin mode")
        assert invalid_sql == []
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
    engine.dispose()


def test_registry_lists_every_approved_router_module() -> None:
    from spooltally.api.registry import PRODUCT_ROUTERS
    from spooltally.api.routers import (
        analytics,
        assets,
        catalog,
        export,
        inventory_queries,
        local_catalog,
        locations,
        metrics,
        refresh,
        restore,
        schemas,
        spool_actions,
        spools,
    )

    assert PRODUCT_ROUTERS == (
        catalog.router,
        schemas.router,
        assets.router,
        refresh.router,
        local_catalog.router,
        spools.router,
        spool_actions.router,
        inventory_queries.router,
        locations.router,
        export.router,
        restore.router,
        analytics.router,
    )
    assert len(PRODUCT_ROUTERS) == 12
    assert all(metrics.router is not router for router in PRODUCT_ROUTERS)
