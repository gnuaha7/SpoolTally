from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers

from spooltally.db.models import Base, SourceFieldOverride
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


def test_inventory_settings_accepts_every_fraction_in_the_approved_range(
    tmp_path: Path,
) -> None:
    """REQ-CORE-010 permits every persisted low-stock fraction in (0, 1]."""
    engine = create_database_engine(tmp_path / "settings.sqlite3")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO inventory_settings "
                "(id, low_stock_numerator, low_stock_denominator) VALUES (1, 1, 2)"
            )
        )
        session.commit()
        assert (
            session.scalar(
                text(
                    "SELECT low_stock_numerator * 1.0 / low_stock_denominator "
                    "FROM inventory_settings"
                )
            )
            == 0.5
        )
    engine.dispose()


def test_json_null_override_is_distinct_from_nullable_sql_snapshot(
    tmp_path: Path,
) -> None:
    """REQ-CAT-004 preserves explicit null, false, zero, and absent overrides."""
    engine = create_database_engine(tmp_path / "json-null.sqlite3")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO local_manufacturers (id, name, created_at, updated_at) "
                "VALUES ('m', 'Maker', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')"
            )
        )
        session.execute(
            text(
                "INSERT INTO local_filaments "
                "(id, manufacturer_id, name, created_at, updated_at) "
                "VALUES ('f', 'm', 'PLA', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')"
            )
        )
        session.add_all(
            [
                SourceFieldOverride(
                    id="o-null",
                    local_filament_id="f",
                    json_pointer="/null",
                    value_json=None,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                SourceFieldOverride(
                    id="o-false",
                    local_filament_id="f",
                    json_pointer="/false",
                    value_json=False,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                SourceFieldOverride(
                    id="o-zero",
                    local_filament_id="f",
                    json_pointer="/zero",
                    value_json=0,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
        assert session.execute(
            text(
                "SELECT json_pointer, value_json FROM source_field_overrides "
                "ORDER BY json_pointer"
            )
        ).all() == [("/false", "false"), ("/null", "null"), ("/zero", "0")]
        assert (
            session.scalar(
                text(
                    "SELECT value_json IS NULL FROM source_field_overrides "
                    "WHERE id = 'o-null'"
                )
            )
            == 0
        )
        assert [
            (override.json_pointer, override.value_json)
            for override in session.scalars(
                select(SourceFieldOverride).order_by(SourceFieldOverride.json_pointer)
            )
        ] == [("/false", False), ("/null", None), ("/zero", 0)]
        assert (
            session.scalar(
                select(SourceFieldOverride.id).where(
                    SourceFieldOverride.json_pointer == "/missing"
                )
            )
            is None
        )
    engine.dispose()


def test_catalog_singletons_digest_and_consumption_reason_are_constrained(
    tmp_path: Path,
) -> None:
    """REQ-CAT-007 and REQ-INV-003 enforce catalog and movement invariants."""
    engine = create_database_engine(tmp_path / "constraints.sqlite3")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO catalog_schedule_state (id, timezone_name) "
                    "VALUES (2, 'UTC')"
                )
            )
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO catalog_runs "
                    "(id, trigger, started_at, status, attempt_count, source_digest) "
                    "VALUES ('r', 'manual', '2026-01-01T00:00:00Z', "
                    "'running', 1, X'01')"
                )
            )
        session.rollback()
        session.execute(
            text(
                "INSERT INTO local_manufacturers (id, name, created_at, updated_at) "
                "VALUES ('m', 'Maker', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')"
            )
        )
        session.execute(
            text(
                "INSERT INTO local_filaments "
                "(id, manufacturer_id, name, created_at, updated_at) "
                "VALUES ('f', 'm', 'PLA', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')"
            )
        )
        session.execute(
            text(
                "INSERT INTO spools (id, filament_id, initial_mg, remaining_mg, "
                "created_at, updated_at) VALUES ('s', 'f', 1000, 1000, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )
        session.commit()
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO spools (id, filament_id, initial_mg, remaining_mg, "
                    "price_eur_cents, created_at, updated_at) "
                    "VALUES ('fractional-price', 'f', 1000, 1000, 1.5, "
                    "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                )
            )
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO movements "
                    "(id, spool_id, kind, occurred_at, amount_mg, reason, "
                    "details_json, before_json, after_json) "
                    "VALUES ('x', 's', 'consumed', '2026-01-01T00:00:00Z', "
                    "1, NULL, '{}', '{}', '{}')"
                )
            )
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO spools (id, filament_id, initial_mg, remaining_mg, "
                    "created_at, updated_at) VALUES ('fractional', 'f', 1.5, 1.5, "
                    "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                )
            )
        session.rollback()
        session.execute(
            text(
                "INSERT INTO spools (id, filament_id, initial_mg, remaining_mg, "
                "created_at, updated_at) VALUES ('max', 'f', "
                "9223372036854775807, 9223372036854775807, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )
        session.commit()
        assert (
            session.scalar(
                text("SELECT typeof(initial_mg) FROM spools WHERE id = 'max'")
            )
            == "integer"
        )
    engine.dispose()
