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
from sqlalchemy import select

from spooltally.api.app import create_app
from spooltally.config import Settings
from spooltally.db.models import (
    Asset,
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

    png = b"\x89PNG\r\n\x1a\nfixture-image"
    image_fail = [False]
    image_requests = []

    def respond(request):
        if request.url.path.endswith("/json/all.json.gz"):
            return httpx.Response(200, stream=httpx.ByteStream(gzip.compress(bulk)))
        if request.url.path.endswith("/schemas/index.json"):
            return httpx.Response(
                200,
                json={
                    "schemas": [
                        {
                            "name": name.removesuffix("_schema"),
                            "file": f"{name}.json",
                            "path": f"{name}.json",
                        }
                        for name in schemas
                    ]
                    + [
                        {
                            "name": "extra display",
                            "file": "extra_schema.json",
                            "path": "extra_schema.json",
                        }
                    ]
                },
            )
        if "/schemas/" in request.url.path:
            name = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            if name == "extra_schema":
                return httpx.Response(200, json={})
            return httpx.Response(200, content=schemas[name])
        if request.url.path.endswith("/brands/logo/index.json"):
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "logos": [
                        {
                            "id": "logo-1",
                            "slug": "logo-1",
                            "brand_id": "brand-a",
                            "brand_name": "Example Brand",
                            "path": "logo-1.json",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/stores/logo/index.json"):
            return httpx.Response(200, json={"count": 0, "logos": []})
        if request.url.path.endswith("/brands/logo/logo-1.json"):
            return httpx.Response(
                200,
                json={
                    "id": "logo-1",
                    "slug": "logo-1",
                    "brand_id": "brand-a",
                    "brand_name": "Example Brand",
                    "path": "logo-1.json",
                    "filename": "logo.png",
                    "extension": "png",
                    "logo_file": "logo-1.png",
                },
            )
        if request.url.path.endswith("/brands/logo/logo-1.png"):
            image_requests.append(dict(request.headers))
            if request.headers.get("if-none-match") == '"asset-v1"':
                if image_fail[0]:
                    return httpx.Response(503)
                return httpx.Response(304)
            if image_fail[0]:
                return httpx.Response(503)
            return httpx.Response(
                200,
                content=png,
                headers={"Content-Type": "image/png", "ETag": '"asset-v1"'},
            )
        return httpx.Response(404)

    app.state.client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=False
    )

    with TestClient(app) as client:

        def refresh():
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
            with app.state.session_factory() as session:
                return run_id, session.get(CatalogRun, run_id).status

        run_id, run_status = refresh()
        assert run_status == "succeeded"
        with app.state.session_factory() as session:
            first_asset = session.scalar(select(Asset))
            assert first_asset is not None
            first_hash, first_blob = first_asset.sha256, first_asset.blob
            assert first_blob == png
            assert first_hash == hashlib.sha256(png).digest()

        second_id, second_status = refresh()
        assert second_status == "succeeded"
        assert any(
            headers.get("if-none-match") == '"asset-v1"' for headers in image_requests
        )
        with app.state.session_factory() as session:
            second_asset = session.scalar(select(Asset))
            assert (second_asset.sha256, second_asset.blob) == (first_hash, first_blob)

        image_fail[0] = True
        third_id, third_status = refresh()
        assert third_status == "succeeded"
        with app.state.session_factory() as session:
            third_asset = session.scalar(select(Asset))
            assert (third_asset.sha256, third_asset.blob) == (first_hash, first_blob)
        assert len({run_id, second_id, third_id}) == 3

    assert all(str(UUID(run_id)) == run_id for run_id in (run_id, second_id, third_id))
    with app.state.session_factory() as session:
        run = session.get(CatalogRun, third_id)
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
