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
    Asset,
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
from spooltally.ofd.logos import LogoResult, LogoSource

FIXTURES = Path(__file__).parent / "fixtures" / "ofd"
PAYLOAD = json.loads((FIXTURES / "bulk.json").read_text())
PAYLOAD["filaments"][0].update(
    {
        "max_bed_temp": 80,
        "min_print_temp": 190,
        "certifications": ["A"],
    }
)
PAYLOAD["filaments"][0]["slicer_settings"]["orcaslicer"] = {"nozzle_temp": 210}
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
    logo_source = LogoSource(
        kind="brand",
        source_id="brand-a",
        index_entry={
            "id": "example-logo",
            "slug": "example-logo",
            "brand_id": "brand-a",
            "brand_name": "Example Brand",
            "path": "example-logo.json",
        },
        metadata={
            "id": "example-logo",
            "slug": "example-logo",
            "brand_id": "brand-a",
            "brand_name": "Example Brand",
            "filename": "example-logo.png",
            "logo_file": "example-logo.png",
            "extension": "png",
        },
    )
    logo_blob = b"\x89PNG\r\n\x1a\nretained source logo"
    logo_result = LogoResult(
        "downloaded",
        "https://api.openfilamentdatabase.org/api/v1/brands/logo/example-logo.png",
        {"index": logo_source.index_entry, "metadata": logo_source.metadata},
        logo_blob,
        "image/png",
        hashlib.sha256(logo_blob).digest(),
    )
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
            ingest_catalog(
                session, PAYLOAD, SCHEMAS, fetched_at=now,
                logos=[(logo_source, logo_result)],
            )
            brand = session.query(CatalogRecord).filter_by(kind="brand").one()
            source = session.query(CatalogRecord).filter_by(kind="filament").one()
            variant = session.query(CatalogRecord).filter_by(kind="variant").one()
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
                selected_variant_id=variant.id,
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
        assert baseline["max_bed_temp"] == 80
        assert baseline["min_print_temp"] == 190
        assert baseline["certifications"] == ["A"]
        assert baseline["slicer_settings"]["orcaslicer"]["nozzle_temp"] == 210
        edit = client.patch(
            f"/api/v1/filaments/{filament.id}",
            json={"default_tare_g": "200", "nominal_weight_g": "750"},
            headers=auth_header
            | {
                "If-Match": client.get(
                    f"/api/v1/filaments/{filament.id}", headers=auth_header
                ).headers["etag"],
                "Idempotency-Key": "default-edit",
            },
        )
        assert edit.status_code == 200, edit.text
        set_values = {
            "/vendor_flag/active": True,
            "/slicer_settings/generic/nozzle_temp": None,
            "/max_bed_temp": None,
            "/certifications": ["C"],
            "/slicer_settings/orcaslicer/nozzle_temp": 220,
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
        refreshed["filaments"][0]["max_bed_temp"] = 90
        refreshed["filaments"][0]["min_print_temp"] = 195
        refreshed["filaments"][0]["certifications"] = ["A", "B"]
        refreshed["filaments"][0]["slicer_settings"]["orcaslicer"][
            "nozzle_temp"
        ] = 215
        with app.state.session_factory() as session:
            with session.begin():
                ingest_catalog(session, refreshed, SCHEMAS, fetched_at=now)
        after_refresh = client.get(effective_url, headers=auth_header).json()
        assert after_refresh["source_baseline_json"]["vendor_flag"]["active"] is False
        assert after_refresh["source_baseline_json"]["max_bed_temp"] == 90
        assert after_refresh["source_baseline_json"]["min_print_temp"] == 195
        assert after_refresh["source_baseline_json"]["certifications"] == ["A", "B"]
        assert (
            after_refresh["source_baseline_json"]["slicer_settings"]["orcaslicer"][
                "nozzle_temp"
            ]
            == 215
        )
        assert after_refresh["effective_source_json"]["vendor_flag"]["active"] is True
        assert (
            after_refresh["effective_source_json"]["slicer_settings"]["generic"][
                "nozzle_temp"
            ]
            is None
        )
        assert after_refresh["effective_source_json"]["max_bed_temp"] is None
        assert after_refresh["effective_source_json"]["certifications"] == ["C"]
        assert (
            after_refresh["effective_source_json"]["slicer_settings"]["orcaslicer"][
                "nozzle_temp"
            ]
            == 220
        )
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
            local = session.get(LocalFilament, filament.id)
            assert (local.default_tare_mg, local.nominal_weight_mg) == (
                200_000,
                750_000,
            )
            assert session.get(InventoryState, 1).dataset_generation == generation
        reset = client.patch(
            f"/api/v1/filaments/{filament.id}/overrides",
            json={
                "reset": [
                    "/vendor_flag/active",
                    "/slicer_settings/generic/nozzle_temp",
                    "/slicer_settings/orcaslicer/nozzle_temp",
                    "/max_bed_temp",
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
        assert effective["max_bed_temp"] == 90
        assert effective["slicer_settings"]["generic"]["nozzle_temp"] == 220
        assert effective["certifications"] == ["A", "B"]
        assert effective["slicer_settings"]["orcaslicer"]["nozzle_temp"] == 215

        source_removed = json.loads(json.dumps(PAYLOAD))
        for collection in ("filaments", "variants", "sizes", "purchase_links"):
            source_removed[collection] = []
        with app.state.session_factory() as session:
            with session.begin():
                ingest_catalog(session, source_removed, SCHEMAS, fetched_at=now)
            source_row = session.query(CatalogRecord).filter_by(kind="filament").one()
            variant_row = session.query(CatalogRecord).filter_by(kind="variant").one()
            assert source_row.id == source.id and variant_row.id == variant.id
            saved_logo = session.query(Asset).one()
            saved_logo_id = saved_logo.id
            saved_logo_hash = saved_logo.sha256
        removed_effective = client.get(effective_url, headers=auth_header)
        assert removed_effective.status_code == 200
        removed_json = removed_effective.json()
        assert removed_json["source_baseline_json"] == effective
        assert (
            removed_json["selected_variant_source_json"]
            == variant.source_baseline_json
        )
        served_logo = client.get(f"/api/v1/assets/{saved_logo_id}", headers=auth_header)
        assert served_logo.status_code == 200
        assert served_logo.content == logo_blob
        assert hashlib.sha256(served_logo.content).digest() == saved_logo_hash
    app.state.engine.dispose()
