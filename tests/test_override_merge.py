from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event

from spooltally.api.app import create_app
from spooltally.config import Settings
from spooltally.db.models import (
    Base,
    CatalogRecord,
    InventoryState,
    LocalFilament,
    LocalManufacturer,
    Spool,
    TokenRecord,
    TokenScope,
)
from spooltally.ofd.ingest import ingest_catalog

FIXTURES = Path(__file__).parent / "fixtures" / "ofd"
PAYLOAD = json.loads((FIXTURES / "bulk.json").read_text())
PAYLOAD["filaments"][0]["certifications"] = ["source"]
SCHEMAS = {
    path.stem: path.read_bytes()
    for path in FIXTURES.glob("*.json")
    if path.name not in {"bulk.json", "fixture_manifest.json"}
}


def test_refresh_preserves_overrides_notes_and_reset_uses_latest_source(tmp_path):
    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    event.listen(
        app.state.engine,
        "connect",
        lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"),
    )
    Base.metadata.create_all(app.state.engine)
    raw_token = bytes([31]) * 32
    token = base64.urlsafe_b64encode(raw_token).decode().rstrip("=")
    now = datetime(2026, 1, 2, tzinfo=UTC)
    generation = str(uuid4())
    with app.state.session_factory() as session:
        with session.begin():
            auth = TokenRecord(
                id=str(uuid4()),
                name="test",
                token_hash=hashlib.sha256(raw_token).digest(),
                created_at=now,
            )
            session.add_all(
                [
                    auth,
                    TokenScope(token=auth, scope="full_access"),
                InventoryState(id=1, dataset_generation=generation),
                ]
            )
            ingest_catalog(session, PAYLOAD, SCHEMAS, fetched_at=now)
            brand = session.query(CatalogRecord).filter_by(kind="brand").one()
            source = session.query(CatalogRecord).filter_by(kind="filament").one()
            maker = LocalManufacturer(
                id=str(uuid4()),
                source_brand_id=brand.id,
                name="Maker",
                notes="maker memo",
                created_at=now,
                updated_at=now,
            )
            filament = LocalFilament(
                id=str(uuid4()),
                manufacturer=maker,
                source_filament_id=source.id,
                selected_variant_id=None,
                name="Local PLA",
                notes="keep this note",
                presentation_json={},
                created_at=now,
                updated_at=now,
            )
            spool = Spool(
                id=str(uuid4()),
                filament=filament,
                tare_mg=180_000,
                initial_mg=1_000_000,
                remaining_mg=500_000,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add_all([maker, filament, spool])

    auth_header = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        effective_url = f"/api/v1/filaments/{filament.id}/effective"
        original = client.get(effective_url, headers=auth_header)
        assert original.status_code == 200
        baseline = original.json()["source_baseline_json"]
        assert baseline["slicer_settings"]["generic"]["nozzle_temp"] == 215
        assert baseline["certifications"] == ["source"]
        set_values = {
            "/vendor_flag/active": True,
            "/slicer_settings/generic/nozzle_temp": None,
            "/certifications": ["local"],
        }
        update = client.patch(
            f"/api/v1/filaments/{filament.id}/overrides",
            json={"set": set_values},
            headers=auth_header
            | {
                "If-Match": client.get(
                    f"/api/v1/filaments/{filament.id}", headers=auth_header
                ).headers["etag"],
                "Idempotency-Key": "override-seed",
            },
        )
        assert update.status_code == 200, update.text
        refreshed = json.loads(json.dumps(PAYLOAD))
        refreshed["filaments"][0]["vendor_flag"]["active"] = False
        refreshed["filaments"][0]["slicer_settings"]["generic"]["nozzle_temp"] = 220
        refreshed["filaments"][0]["certifications"] = ["source"]
        with app.state.session_factory() as session:
            with session.begin():
                ingest_catalog(session, refreshed, SCHEMAS, fetched_at=now)
        after_refresh = client.get(effective_url, headers=auth_header).json()
        assert after_refresh["source_baseline_json"]["vendor_flag"]["active"] is False
        assert after_refresh["effective_source_json"]["vendor_flag"]["active"] is True
        assert (
            after_refresh["effective_source_json"]["slicer_settings"]["generic"][
                "nozzle_temp"
            ]
            is None
        )
        assert after_refresh["effective_source_json"]["certifications"] == ["local"]
        current = client.get(f"/api/v1/filaments/{filament.id}", headers=auth_header)
        assert current.json()["notes"] == "keep this note"
        with app.state.session_factory() as session:
            stored = session.get(Spool, spool.id)
            assert stored is not None
            assert (stored.tare_mg, stored.initial_mg, stored.remaining_mg) == (
                180_000,
                1_000_000,
                500_000,
            )
            assert session.get(InventoryState, 1).dataset_generation == generation
        reset = client.patch(
            f"/api/v1/filaments/{filament.id}/overrides",
            json={
                "reset": [
                    "/vendor_flag/active",
                    "/slicer_settings/generic/nozzle_temp",
                    "/certifications",
                ]
            },
            headers=auth_header
            | {
                "If-Match": current.headers["etag"],
                "Idempotency-Key": "override-reset",
            },
        )
        assert reset.status_code == 200, reset.text
        effective = client.get(effective_url, headers=auth_header).json()[
            "effective_source_json"
        ]
        assert effective["vendor_flag"]["active"] is False
        assert effective["slicer_settings"]["generic"]["nozzle_temp"] == 220
        assert effective["certifications"] == ["source"]
    app.state.engine.dispose()
