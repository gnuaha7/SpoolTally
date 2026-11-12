from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from spooltally.api.app import create_app
from spooltally.config import Settings
from spooltally.db.models import (
    Base,
    CatalogRecord,
    CatalogRecordLink,
    CatalogSchema,
    InventoryState,
    LocalFilament,
    LocalManufacturer,
    TokenRecord,
    TokenScope,
)


@pytest.fixture
def catalog_client(tmp_path):
    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app.state.engine)
    raw = bytes([8]) * 32
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    now = datetime(2026, 1, 2, tzinfo=UTC)
    ids = [str(uuid4()) for _ in range(7)]
    with app.state.session_factory() as session:
        record = TokenRecord(
            id=str(uuid4()),
            name="test",
            token_hash=hashlib.sha256(raw).digest(),
            created_at=now,
        )
        session.add_all(
            [
                record,
                TokenScope(token=record, scope="full_access"),
                InventoryState(id=1, dataset_generation=str(uuid4())),
            ]
        )
        for kind, rid in zip(
            (
                "brand",
                "material",
                "filament",
                "variant",
                "size",
                "store",
                "purchase_link",
            ),
            ids,
            strict=True,
        ):
            session.add(
                CatalogRecord(
                    id=rid,
                    kind=kind,
                    source_id=f"src-{kind}",
                    identity_key=f"{kind}:src-{kind}",
                    upstream_uuid=None,
                    parent_id=None,
                    source_baseline_json={"zero": 0, "false": False, "null": None},
                    typed_fields_json={"known": [1, 2]},
                    extension_json={"future": {"x": True}},
                    raw_json={"source": kind, "unknown": "kept"},
                    source_updated_at=None,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        session.add(
            CatalogRecordLink(
                id=str(uuid4()),
                source_record_id=ids[2],
                relationship_name="variant",
                ordinal=0,
                target_record_id=ids[3],
            )
        )
        session.commit()
    with TestClient(app) as client:
        yield client, {"Authorization": f"Bearer {token}"}, app, ids
    app.state.engine.dispose()


def test_catalog_round_trip_preserves_all_source_fields(catalog_client):
    client, auth, app, ids = catalog_client
    with app.state.session_factory() as session:
        generation = session.get(InventoryState, 1).dataset_generation
    response = client.get("/api/v1/catalog/records", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 7
    assert [r["id"] for r in body["items"]] == sorted(ids)
    assert {r["kind"] for r in body["items"]} == {
        "brand",
        "material",
        "filament",
        "variant",
        "size",
        "store",
        "purchase_link",
    }
    item = client.get(f"/api/v1/catalog/records/{ids[2]}", headers=auth).json()
    assert item == {
        "id": ids[2],
        "kind": "filament",
        "source_id": "src-filament",
        "identity_key": "filament:src-filament",
        "upstream_uuid": None,
        "parent_id": None,
        "source_baseline_json": {"zero": 0, "false": False, "null": None},
        "typed_fields_json": {"known": [1, 2]},
        "extension_json": {"future": {"x": True}},
        "raw_json": {"source": "filament", "unknown": "kept"},
        "source_updated_at": None,
        "first_seen_at": "2026-01-02T00:00:00Z",
        "last_seen_at": "2026-01-02T00:00:00Z",
        "links": [
            {"relationship_name": "variant", "ordinal": 0, "target_record_id": ids[3]}
        ],
    }
    assert (
        client.get(
            "/api/v1/catalog/records?kind=material&limit=01", headers=auth
        ).json()["total"]
        == 1
    )
    for query in (
        "kind=alien",
        "limit=999999999999999999999999999999999999999999999",
        "offset=１２",
    ):
        assert (
            client.get(f"/api/v1/catalog/records?{query}", headers=auth).status_code
            == 422
        )
    assert (
        client.get(f"/api/v1/catalog/records/{uuid4()}", headers=auth).json()["error"][
            "code"
        ]
        == "not_found"
    )
    assert client.get("/api/v1/catalog/records").status_code == 401
    assert (
        client.get("/api/v1/catalog/records", headers=auth).headers["content-type"]
        == "application/json"
    )
    assert "etag" not in client.get("/api/v1/catalog/records", headers=auth).headers
    with app.state.session_factory() as session:
        assert session.query(CatalogRecord).count() == 7
        assert session.query(LocalFilament).count() == 0
        assert session.get(InventoryState, 1).dataset_generation == generation


def test_catalog_name_change_keeps_uuid_reference(tmp_path):
    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app.state.engine)
    now = datetime(2026, 1, 2, tzinfo=UTC)
    raw = bytes([7]) * 32
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    filament_id = str(uuid4())
    local_id = str(uuid4())
    with app.state.session_factory() as session:
        auth = TokenRecord(
            id=str(uuid4()),
            name="test",
            token_hash=hashlib.sha256(raw).digest(),
            created_at=now,
        )
        brand = CatalogSchema(
            schema_name="brand",
            source_version="1",
            source_json={"unknown": [0, False, None]},
            fetched_at=now,
        )
        source = CatalogRecord(
            id=filament_id,
            kind="filament",
            source_id="vendor-1",
            identity_key="filament:vendor-1",
            upstream_uuid=None,
            parent_id=None,
            source_baseline_json={},
            typed_fields_json={},
            extension_json={},
            raw_json={"name": "Old"},
            source_updated_at=None,
            first_seen_at=now,
            last_seen_at=now,
        )
        maker = LocalManufacturer(
            id=str(uuid4()),
            source_brand_id=None,
            name="Maker",
            notes="",
            revision=1,
            created_at=now,
            updated_at=now,
        )
        filament = LocalFilament(
            id=local_id,
            manufacturer_id=maker.id,
            source_filament_id=filament_id,
            selected_variant_id=None,
            name="My spool",
            presentation_json={},
            notes="",
            default_tare_mg=None,
            nominal_weight_mg=None,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add_all(
            [
                auth,
                TokenScope(token=auth, scope="full_access"),
                InventoryState(id=1, dataset_generation=str(uuid4())),
                brand,
                source,
                maker,
                filament,
            ]
        )
        session.commit()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/catalog/schemas", headers=headers).json() == {
            "items": [
                {
                    "schema_name": "brand",
                    "source_version": "1",
                    "fetched_at": "2026-01-02T00:00:00Z",
                    "source_json": {"unknown": [0, False, None]},
                }
            ]
        }
        assert (
            client.get("/api/v1/catalog/schemas/missing", headers=headers).json()[
                "error"
            ]["code"]
            == "not_found"
        )
        with app.state.session_factory() as session:
            source = session.get(CatalogRecord, filament_id)
            assert source is not None
            source.raw_json = {"name": "New"}
            session.commit()
        fetched = client.get(
            f"/api/v1/catalog/records/{filament_id}", headers=headers
        ).json()
        assert fetched["id"] == filament_id and fetched["raw_json"] == {"name": "New"}
        with app.state.session_factory() as session:
            assert (
                session.get(LocalFilament, local_id).source_filament_id == filament_id
            )
    app.state.engine.dispose()
