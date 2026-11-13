from __future__ import annotations

import base64
import gzip
import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from spooltally.api.app import create_app
from spooltally.config import Settings
from spooltally.db.models import (
    Base,
    CatalogRun,
    CatalogScheduleState,
    InventoryState,
    TokenRecord,
    TokenScope,
)


def test_manual_refresh_returns_durable_accepted_run(tmp_path):
    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app.state.engine)
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    raw_token = bytes([17]) * 32
    token = base64.urlsafe_b64encode(raw_token).decode().rstrip("=")
    with app.state.session_factory() as session:
        record = TokenRecord(
            id="7ecb2f4d-100b-4e8f-82ac-958b6248fd55",
            name="refresh test",
            token_hash=hashlib.sha256(raw_token).digest(),
            created_at=now,
        )
        session.add_all(
            [
                record,
                TokenScope(token=record, scope="full_access"),
                InventoryState(
                    id=1, dataset_generation="11111111-1111-4111-8111-111111111111"
                ),
                CatalogScheduleState(
                    id=1,
                    timezone_name="Europe/Madrid",
                    last_handled_slot_key="2026-11-13@03:00",
                ),
            ]
        )
        session.commit()
    app.state.clock = lambda: now
    fixture_dir = Path(__file__).parent / "fixtures" / "ofd"
    bulk = (fixture_dir / "bulk.json").read_bytes()
    schemas = {
        path.name.removesuffix(".json"): path.read_bytes()
        for path in fixture_dir.glob("*_schema.json")
    }
    schemas["trait_rules"] = (fixture_dir / "trait_rules.json").read_bytes()

    def respond(request):
        if request.url.path.endswith("/json/all.json.gz"):
            return httpx.Response(200, content=gzip.compress(bulk))
        if request.url.path.endswith("/schemas/index.json"):
            return httpx.Response(200, json={"schemas": []})
        if "/schemas/" in request.url.path:
            name = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            return httpx.Response(200, content=schemas[name])
        if "/logos/index.json" in request.url.path:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    app.state.client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=False
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/refresh", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with app.state.session_factory() as session:
                run = session.get(CatalogRun, run_id)
                if run.status != "running":
                    break
            time.sleep(0.01)

    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert str(UUID(run_id)) == run_id
    with app.state.session_factory() as session:
        run = session.get(CatalogRun, run_id)
        assert run is not None
        assert run.trigger == "manual"
        assert run.status == "succeeded"
        assert run.source_digest is not None and len(run.source_digest) == 32
        assert session.get(InventoryState, 1).dataset_generation == (
            "11111111-1111-4111-8111-111111111111"
        )
    app.state.engine.dispose()


def test_lifespan_lock_allows_only_one_refresh_owner(tmp_path):
    from spooltally.db.models import CatalogRun

    app1 = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app1.state.engine)
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    with app1.state.session_factory() as session:
        session.add(
            CatalogScheduleState(
                id=1,
                timezone_name="Europe/Madrid",
                last_handled_slot_key="2026-11-13@03:00",
            )
        )
        session.add(
            CatalogRun(
                id="aa1e898b-ae07-4e96-8b15-bfd39e05de7b",
                trigger="manual",
                schedule_key=None,
                started_at=now,
                status="running",
                attempt_count=1,
            )
        )
        session.commit()
    app1.state.clock = lambda: now
    app1.state.client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"bad")
        )
    )
    app2 = create_app(Settings(tmp_path, "127.0.0.1", 0))
    try:
        with TestClient(app1):
            assert app1.state.refresh_owner is True
            with TestClient(app2):
                assert app2.state.refresh_owner is False
        with app1.state.session_factory() as session:
            run = session.get(CatalogRun, "aa1e898b-ae07-4e96-8b15-bfd39e05de7b")
            assert run.attempt_count == 2
    finally:
        app1.state.engine.dispose()
        app2.state.engine.dispose()
