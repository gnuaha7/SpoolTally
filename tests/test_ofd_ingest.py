from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from spooltally.db.models import (
    Asset,
    Base,
    CatalogRecord,
    CatalogRecordLink,
    CatalogRun,
    CatalogSchema,
    LocalFilament,
    LocalManufacturer,
)
from spooltally.ofd.adapter import AdapterError
from spooltally.ofd.ingest import ingest_catalog
from spooltally.ofd.logos import LogoResult, LogoSource

FIXTURES = Path(__file__).parent / "fixtures" / "ofd"
PAYLOAD = json.loads((FIXTURES / "bulk.json").read_text())
SCHEMAS = {
    path.stem: path.read_bytes()
    for path in FIXTURES.glob("*.json")
    if path.name not in {"bulk.json", "fixture_manifest.json"}
}
NOW = datetime(2026, 1, 2, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    value = create_engine(f"sqlite:///{tmp_path / 'ingest.sqlite3'}")
    event.listen(
        value, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON")
    )
    Base.metadata.create_all(value)
    yield value
    value.dispose()


def test_ingests_all_source_kinds_links_and_schema_documents(engine):
    with Session(engine) as session:
        with session.begin():
            ingest_catalog(session, PAYLOAD, SCHEMAS, fetched_at=NOW)
        assert {row.kind for row in session.scalars(select(CatalogRecord))} == {
            "brand",
            "material",
            "filament",
            "variant",
            "size",
            "store",
            "purchase_link",
        }
        assert session.scalar(
            select(CatalogRecord).where(CatalogRecord.kind == "filament")
        ).source_baseline_json["vendor_flag"] == {"active": False}
        links = {
            (row.relationship_name, row.source_record_id, row.target_record_id)
            for row in session.scalars(select(CatalogRecordLink))
        }
        rows = {row.kind: row for row in session.scalars(select(CatalogRecord))}
        assert links == {
            ("brand_id", rows["material"].id, rows["brand"].id),
            ("material_id", rows["filament"].id, rows["material"].id),
            ("brand_id", rows["filament"].id, rows["brand"].id),
            ("filament_id", rows["variant"].id, rows["filament"].id),
            ("variant_id", rows["size"].id, rows["variant"].id),
            ("size_id", rows["purchase_link"].id, rows["size"].id),
            ("store_id", rows["purchase_link"].id, rows["store"].id),
        }
        assert {
            row.schema_name for row in session.scalars(select(CatalogSchema))
        } == set(SCHEMAS)


def test_invalid_catalog_does_not_change_prior_committed_snapshot(engine):
    with Session(engine) as session:
        with session.begin():
            ingest_catalog(session, PAYLOAD, SCHEMAS, fetched_at=NOW)
        with Session(engine) as reader:
            before = [
                (row.id, row.raw_json)
                for row in reader.scalars(
                    select(CatalogRecord).order_by(CatalogRecord.id)
                )
            ]
        invalid = json.loads(json.dumps(PAYLOAD))
        invalid["filaments"][0]["material_id"] = "missing"
        with pytest.raises(AdapterError):
            with session.begin():
                ingest_catalog(session, invalid, SCHEMAS, fetched_at=NOW)
        after = [
            (row.id, row.raw_json)
            for row in session.scalars(select(CatalogRecord).order_by(CatalogRecord.id))
        ]
        assert after == before


def test_ingest_participates_in_callers_rollback(engine):
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="caller abort"):
            with session.begin():
                ingest_catalog(session, PAYLOAD, SCHEMAS, fetched_at=NOW)
                assert session.scalar(select(CatalogRecord.id)) is not None
                raise RuntimeError("caller abort")
        assert session.scalar(select(CatalogRecord.id)) is None
        assert session.scalar(select(CatalogSchema.schema_name)) is None


def test_reapplying_snapshot_keeps_ids_and_first_seen_and_refreshes_values(engine):
    changed = json.loads(json.dumps(PAYLOAD))
    changed["filaments"][0]["name"] = "Updated PLA"
    with Session(engine) as session:
        with session.begin():
            ingest_catalog(session, PAYLOAD, SCHEMAS, fetched_at=NOW)
        original = session.scalar(
            select(CatalogRecord).where(CatalogRecord.kind == "filament")
        )
        original_id, first_seen = original.id, original.first_seen_at
        session.rollback()
        with session.begin():
            ingest_catalog(
                session, changed, SCHEMAS, fetched_at=datetime(2026, 1, 3, tzinfo=UTC)
            )
        updated = session.scalar(
            select(CatalogRecord).where(CatalogRecord.kind == "filament")
        )
        assert (
            updated.id,
            updated.first_seen_at,
            updated.source_baseline_json["name"],
        ) == (original_id, first_seen, "Updated PLA")


def test_absent_source_graph_and_logo_are_retained(engine):
    logo_id = "example-logo"
    source = LogoSource(
        kind="brand",
        source_id="brand-a",
        index_entry={
            "id": logo_id,
            "slug": logo_id,
            "brand_id": "brand-a",
            "brand_name": "Example Brand",
            "path": f"{logo_id}.json",
        },
        metadata={
            "id": logo_id,
            "slug": logo_id,
            "brand_id": "brand-a",
            "brand_name": "Example Brand",
            "filename": f"{logo_id}.png",
            "logo_file": f"{logo_id}.png",
            "extension": "png",
        },
    )
    blob = b"\x89PNG\r\n\x1a\nlogo"
    result = LogoResult(
        "downloaded",
        "https://api.openfilamentdatabase.org/api/v1/brands/logo/example-logo.png",
        {"index": source.index_entry, "metadata": source.metadata},
        blob,
        "image/png",
        __import__("hashlib").sha256(blob).digest(),
    )
    with Session(engine) as session:
        with session.begin():
            ingest_catalog(
                session, PAYLOAD, SCHEMAS, fetched_at=NOW, logos=[(source, result)]
            )
        brand_id = session.scalar(
            select(CatalogRecord.id).where(CatalogRecord.kind == "brand")
        )
        logo = session.scalar(select(Asset).where(Asset.source_record_id == brand_id))
        assert logo is not None and logo.blob == blob
        session.rollback()
        with session.begin():
            ingest_catalog(
                session,
                {
                    **PAYLOAD,
                    "brands": [],
                    "materials": [],
                    "filaments": [],
                    "variants": [],
                    "sizes": [],
                    "purchase_links": [],
                },
                SCHEMAS,
                fetched_at=datetime(2026, 1, 3, tzinfo=UTC),
            )
        retained = session.get(CatalogRecord, brand_id)
        retained_logo = session.scalar(
            select(Asset).where(Asset.source_record_id == brand_id)
        )
        assert retained is not None and retained.raw_json["name"] == "Example Brand"
        assert retained_logo is not None and retained_logo.blob == blob


def test_uuid_name_change_preserves_source_identity(engine):
    first = json.loads(json.dumps(PAYLOAD))
    first["brands"][0]["uuid"] = "12345678-1234-5678-1234-567812345678"
    renamed = json.loads(json.dumps(first))
    renamed["brands"][0]["name"] = "Renamed Brand"
    with Session(engine) as session:
        with session.begin():
            ingest_catalog(session, first, SCHEMAS, fetched_at=NOW)
        original = session.scalar(
            select(CatalogRecord).where(CatalogRecord.kind == "brand")
        )
        source_id = original.id
        session.rollback()
        maker = LocalManufacturer(
            id="10000000-0000-4000-8000-000000000001",
            source_brand_id=source_id,
            name="Maker",
            created_at=NOW,
            updated_at=NOW,
        )
        source_filament_id = session.scalar(
            select(CatalogRecord.id).where(CatalogRecord.kind == "filament")
        )
        session.rollback()
        with session.begin():
            session.add(maker)
            session.add(
                LocalFilament(
                    id="10000000-0000-4000-8000-000000000002",
                    manufacturer=maker,
                    source_filament_id=source_filament_id,
                    name="Local filament",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        with session.begin():
            ingest_catalog(
                session, renamed, SCHEMAS, fetched_at=datetime(2026, 1, 3, tzinfo=UTC)
            )
        updated = session.get(CatalogRecord, source_id)
        assert updated is not None and updated.raw_json["name"] == "Renamed Brand"
        assert session.query(CatalogRecord).filter_by(kind="brand").count() == 1
        assert (
            session.get(
                LocalFilament, "10000000-0000-4000-8000-000000000002"
            ).source_filament_id
            == source_filament_id
        )


def test_late_write_failure_rolls_back_catalog_schemas_asset_and_caller_row(engine):
    blob = b"\x89PNG\r\n\x1a\nlogo"
    source = LogoSource(
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
    result = LogoResult(
        "downloaded",
        "https://api.openfilamentdatabase.org/api/v1/brands/logo/example-logo.png",
        {"index": source.index_entry, "metadata": source.metadata},
        blob,
        "image/png",
        __import__("hashlib").sha256(blob).digest(),
    )

    def fail_run_insert(mapper, connection, target):
        raise RuntimeError("simulated late write failure")

    event.listen(CatalogRun, "before_insert", fail_run_insert)
    try:
        with Session(engine) as session:
            with pytest.raises(RuntimeError, match="simulated late write failure"):
                with session.begin():
                    ingest_catalog(
                        session,
                        PAYLOAD,
                        SCHEMAS,
                        fetched_at=NOW,
                        logos=[(source, result)],
                    )
                    session.flush()
                    session.add(
                        CatalogRun(
                            id="12345678-1234-5678-1234-567812345678",
                            trigger="manual",
                            schedule_key=None,
                            started_at=NOW,
                            status="running",
                            attempt_count=1,
                        )
                    )
                    session.flush()
            assert session.scalar(select(CatalogRecord.id)) is None
            assert session.scalar(select(CatalogSchema.schema_name)) is None
            assert session.scalar(select(Asset.id)) is None
            assert session.scalar(select(CatalogRun.id)) is None
    finally:
        event.remove(CatalogRun, "before_insert", fail_run_insert)
