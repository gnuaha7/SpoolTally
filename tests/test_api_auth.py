from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from spooltally.api.app import create_app
from spooltally.api.auth import Principal, require_full_access
from spooltally.config import Settings
from spooltally.db.models import Base, TokenRecord, TokenScope
from spooltally.db.session import create_engine, get_session

TOKEN = base64.urlsafe_b64encode(bytes(32)).decode().rstrip("=")
ACTIVE_TOKEN = base64.urlsafe_b64encode(bytes([2]) * 32).decode().rstrip("=")
UNKNOWN_TOKEN = base64.urlsafe_b64encode(bytes([3]) * 32).decode().rstrip("=")
TOKEN_ID = "02a1a27a-5501-7793-ae9f-32e57df6e409"
UNAUTHORIZED_BODY = (
    '{"error":{"code":"unauthorized","message":"Authentication is required."}}'
)


def create_test_app() -> FastAPI:
    app = create_app(Settings(Path(os.environ["SPOOLTALLY_TEST_DIR"]), "127.0.0.1", 0))

    @app.post("/test/protected-mutation")
    def mutate(
        principal: Annotated[Principal, Depends(require_full_access)],
        session: Annotated[Session, Depends(get_session)],
    ) -> dict[str, str]:
        assert UUID(principal.token_id)
        assert principal.scopes == ("full_access",)
        assert not session.in_transaction()
        session.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
        session.execute(text("UPDATE auth_marker SET value = value + 1"))
        session.execute(
            text("INSERT INTO auth_history (token_id) VALUES (:id)"),
            {"id": principal.token_id},
        )
        session.commit()
        return {"status": "changed"}

    return app


def _request(url: str, authorization: str | None) -> tuple[int, str]:
    headers = {} if authorization is None else {"Authorization": authorization}
    request = urllib.request.Request(url, method="POST", headers=headers, data=b"")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as response:
        return response.code, response.read().decode()


def test_missing_and_revoked_token_return_401_without_effects(tmp_path: Path) -> None:
    from spooltally.api.auth import Principal

    principal = Principal(TOKEN_ID, ("full_access",))
    assert principal.scopes == ("full_access",)
    with pytest.raises((AttributeError, TypeError)):
        principal.token_id = "changed"

    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app.state.engine)
    with app.state.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE auth_marker (value INTEGER NOT NULL)")
        connection.exec_driver_sql("CREATE TABLE auth_history (token_id TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO auth_marker VALUES (0)")
    with app.state.session_factory() as session:
        revoked = TokenRecord(
            id=TOKEN_ID,
            name="revoked",
            token_hash=hashlib.sha256(bytes(32)).digest(),
            created_at=datetime.now(UTC),
            revoked_at=datetime.now(UTC),
        )
        session.add(revoked)
        session.add(TokenScope(token=revoked, scope="full_access"))
        active = TokenRecord(
            id="12a1a27a-5501-7793-ae9f-32e57df6e409",
            name="active",
            token_hash=hashlib.sha256(bytes([2]) * 32).digest(),
            created_at=datetime.now(UTC),
        )
        session.add(active)
        session.add(TokenScope(token=active, scope="full_access"))
        session.commit()
    app.state.engine.dispose()

    repo = Path(__file__).resolve().parents[1]
    env = os.environ | {
        "SPOOLTALLY_TEST_DIR": str(tmp_path),
        "PYTHONPATH": f"{repo / 'src'}:{repo / 'tests'}",
    }
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "test_api_auth:create_test_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}/test/protected-mutation"
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError("Uvicorn exited before becoming ready")
            try:
                _request(url, None)
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("Uvicorn did not become ready")

        invalid = (
            None,
            "Basic abc",
            "Bearer " + "A" * 42,
            "Bearer " + TOKEN + "=",
            "Bearer " + TOKEN[:-1] + "B",
            "Bearer " + UNKNOWN_TOKEN,
        )
        for authorization in invalid:
            status, body = _request(url, authorization)
            assert status == 401
            assert body == UNAUTHORIZED_BODY
        status, body = _request(url, "Bearer " + TOKEN)
        assert status == 401
        assert body == UNAUTHORIZED_BODY
        inspection_engine = create_engine(tmp_path / "spooltally.sqlite3")
        with inspection_engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT value FROM auth_marker").scalar_one()
                == 0
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM auth_history"
                ).scalar_one()
                == 0
            )
        status, body = _request(url, "Bearer " + ACTIVE_TOKEN)
        assert status == 200 and body == '{"status":"changed"}'
        with inspection_engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT value FROM auth_marker").scalar_one()
                == 1
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM auth_history"
                ).scalar_one()
                == 1
            )
        inspection_engine.dispose()
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        assert all(
            secret not in stdout + stderr
            for secret in (TOKEN, ACTIVE_TOKEN, UNKNOWN_TOKEN)
        )
        assert hashlib.sha256(bytes(32)).hexdigest() not in stdout + stderr
        assert hashlib.sha256(bytes([2]) * 32).hexdigest() not in stdout + stderr
