from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers

from spooltally.db.models import Base
from spooltally.db.session import create_engine as create_database_engine

EXPECTED_TABLES = {
    "catalog_records",
    "catalog_record_links",
    "catalog_schemas",
    "local_manufacturers",
    "local_filaments",
    "source_field_overrides",
    "locations",
    "spools",
    "movements",
    "token_records",
    "token_scopes",
    "idempotency_records",
    "assets",
    "inventory_state",
    "restore_control",
    "inventory_settings",
    "catalog_schedule_state",
    "catalog_runs",
}


def test_metadata_creates_the_frozen_table_set_on_sqlite(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "schema.sqlite3")
    Base.metadata.create_all(engine)
    configure_mappers()
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == EXPECTED_TABLES
    assert all(
        foreign_key["options"]["ondelete"] == "RESTRICT"
        for table in EXPECTED_TABLES
        for foreign_key in inspector.get_foreign_keys(table)
    )
    engine.dispose()


def test_sqlite_enforces_catalog_identity_uniqueness(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "identity.sqlite3")
    Base.metadata.create_all(engine)
    insert = text("""
        INSERT INTO catalog_records
            (id, kind, source_id, identity_key, source_baseline_json,
             typed_fields_json, extension_json, raw_json, first_seen_at, last_seen_at)
        VALUES (:id, 'filament', :source, 'ofd:filament:raw-id', '{}', '{}', '{}', '{}',
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
    """)
    with Session(engine) as session:
        session.execute(
            insert, {"id": "00000000-0000-4000-8000-000000000001", "source": "raw-id"}
        )
        session.commit()
        with pytest.raises(IntegrityError):
            session.execute(
                insert,
                {"id": "00000000-0000-4000-8000-000000000002", "source": "other"},
            )
    engine.dispose()


def test_sqlite_restricts_deleting_a_referenced_manufacturer(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "restrict.sqlite3")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO local_manufacturers (id, name, notes, revision, created_at, updated_at) VALUES ('00000000-0000-4000-8000-000000000003', 'Maker', '', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"  # noqa: E501
            )
        )
        session.execute(
            text(
                "INSERT INTO local_filaments (id, manufacturer_id, name, presentation_json, notes, revision, created_at, updated_at) VALUES ('00000000-0000-4000-8000-000000000004', '00000000-0000-4000-8000-000000000003', 'PLA', '{}', '', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"  # noqa: E501
            )
        )
        session.commit()
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "DELETE FROM local_manufacturers WHERE id = '00000000-0000-4000-8000-000000000003'"  # noqa: E501
                )
            )
    engine.dispose()
