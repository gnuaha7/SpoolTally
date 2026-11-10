import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from spooltally.config import Settings
from spooltally.db import base
from spooltally.db.base import initialize_database, set_restore_mode
from spooltally.db.session import create_engine


def test_init_migrates_and_restart_preserves_generation(tmp_path: Path) -> None:
    settings = Settings(tmp_path, "127.0.0.1", 8000)
    initialize_database(settings)
    engine = create_engine(settings.database_path)
    with engine.connect() as connection:
        generation = connection.execute(
            text("SELECT dataset_generation FROM inventory_state WHERE id=1")
        ).scalar_one()
        assert len(inspect(connection).get_table_names()) == 19
    engine.dispose()
    initialize_database(settings)
    engine = create_engine(settings.database_path)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT dataset_generation FROM inventory_state WHERE id=1")
            ).scalar_one()
            == generation
        )
        assert connection.execute(
            text(
                "SELECT low_stock_numerator, low_stock_denominator "
                "FROM inventory_settings WHERE id=1"
            )
        ).one() == (1, 10)
    engine.dispose()


def test_restore_mode_requires_transaction_and_valid_mode(tmp_path: Path) -> None:
    settings = Settings(tmp_path, "127.0.0.1", 8000)
    initialize_database(settings)
    engine = create_engine(settings.database_path)
    with engine.connect() as connection:
        try:
            set_restore_mode(connection, "restore")
        except RuntimeError:
            pass
        else:
            raise AssertionError("restore mode changed outside a transaction")
        transaction = connection.begin()
        try:
            set_restore_mode(connection, "restore")
            assert (
                connection.execute(
                    text("SELECT mode FROM restore_control WHERE id=1")
                ).scalar_one()
                == "restore"
            )
            transaction.rollback()
        except Exception:
            transaction.rollback()
            raise
        assert (
            connection.execute(
                text("SELECT mode FROM restore_control WHERE id=1")
            ).scalar_one()
            == "normal"
        )
    engine.dispose()


def test_existing_database_gets_private_verified_pre_migration_copy(
    tmp_path: Path,
) -> None:
    settings = Settings(tmp_path, "127.0.0.1", 8000)
    with sqlite3.connect(settings.database_path) as database:
        database.execute("CREATE TABLE legacy (value TEXT)")
        database.execute("INSERT INTO legacy VALUES ('preserved')")

    initialize_database(settings)

    backups = list((tmp_path / "backups" / "pre-migration").glob("*.sqlite3"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert database.execute("SELECT value FROM legacy").fetchone() == ("preserved",)


def test_backup_failure_prevents_migration_and_data_change(tmp_path, monkeypatch):
    settings = Settings(tmp_path, "127.0.0.1", 8000)
    with sqlite3.connect(settings.database_path) as database:
        database.execute("CREATE TABLE legacy (value TEXT)")
        database.execute("INSERT INTO legacy VALUES ('before')")

    def fail_backup(*args, **kwargs):
        raise OSError("injected backup failure")

    monkeypatch.setattr(base, "backup_before_migration", fail_backup)
    with pytest.raises(OSError, match="injected backup failure"):
        initialize_database(settings)

    with sqlite3.connect(settings.database_path) as database:
        assert database.execute("SELECT value FROM legacy").fetchone() == ("before",)
        assert (
            database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_records'"
            ).fetchone()
            is None
        )


def test_failed_migration_rolls_back_schema_and_keeps_verified_backup(
    tmp_path, monkeypatch
):
    settings = Settings(tmp_path, "127.0.0.1", 8000)
    with sqlite3.connect(settings.database_path) as database:
        database.execute("CREATE TABLE legacy (value TEXT)")
        database.execute("INSERT INTO legacy VALUES ('before')")
    real_upgrade = base.command.upgrade

    def upgrade_then_fail(config, revision):
        real_upgrade(config, revision)
        raise RuntimeError("injected post-DDL failure")

    monkeypatch.setattr(base.command, "upgrade", upgrade_then_fail)
    with pytest.raises(RuntimeError, match="injected post-DDL failure"):
        initialize_database(settings)

    backups = list((tmp_path / "backups" / "pre-migration").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(settings.database_path) as database:
        assert database.execute("SELECT value FROM legacy").fetchone() == ("before",)
        assert (
            database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_records'"
            ).fetchone()
            is None
        )
    with sqlite3.connect(backups[0]) as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert database.execute("SELECT value FROM legacy").fetchone() == ("before",)


@pytest.mark.parametrize("operation", ["pre-migration", "pre-restore"])
def test_retention_uses_timestamps_and_keeps_newest_five(tmp_path, operation):
    directory = tmp_path / "backups" / operation
    directory.mkdir(parents=True)
    names = []
    for index in range(7):
        timestamp = f"20260101T00000{index}.000000Z"
        prefix = (
            f"0001_initial-{timestamp}-{uuid.uuid4()}"
            if operation == "pre-migration"
            else f"{uuid.uuid4()}-{timestamp}"
        )
        path = directory / f"{prefix}.sqlite3"
        with sqlite3.connect(path) as database:
            database.execute("CREATE TABLE marker (value INTEGER)")
            database.execute("INSERT INTO marker VALUES (?)", (index,))
        names.append(path)

    base.prune_recovery_backups(directory)

    assert {path.name for path in directory.glob("*.sqlite3")} == {
        path.name for path in names[2:]
    }


def test_movement_triggers_allow_changes_only_in_transactional_restore_mode(tmp_path):
    settings = Settings(tmp_path, "127.0.0.1", 8000)
    initialize_database(settings)
    engine = create_engine(settings.database_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO local_manufacturers (id,name,created_at,updated_at) VALUES ('maker','Maker','now','now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO local_filaments (id,manufacturer_id,name,created_at,updated_at) VALUES ('filament','maker','PLA','now','now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO spools (id,filament_id,initial_mg,remaining_mg,created_at,updated_at) VALUES ('spool','filament',100,100,'now','now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO movements (id,spool_id,kind,occurred_at,after_json) VALUES ('movement','spool','created','now','{}')"
            )
        )
    with engine.connect() as connection:
        with pytest.raises(Exception, match="movement_append_only"):
            connection.execute(
                text("UPDATE movements SET occurred_at='later' WHERE id='movement'")
            )
        connection.rollback()
        transaction = connection.begin()
        set_restore_mode(connection, "restore")
        connection.execute(
            text("UPDATE movements SET occurred_at='restored' WHERE id='movement'")
        )
        connection.execute(text("DELETE FROM movements WHERE id='movement'"))
        transaction.rollback()
        assert (
            connection.execute(
                text("SELECT occurred_at FROM movements WHERE id='movement'")
            ).scalar_one()
            == "now"
        )
        assert (
            connection.execute(
                text("SELECT mode FROM restore_control WHERE id=1")
            ).scalar_one()
            == "normal"
        )
    engine.dispose()


def test_init_persists_reduced_fraction_and_invalid_cli_value_has_no_effect(
    tmp_path, run_cli
):
    data_dir = tmp_path / "fraction"
    result = run_cli(
        "init",
        "--data-dir",
        str(data_dir),
        env={"SPOOLTALLY_LOW_STOCK_FRACTION": "0.125"},
    )
    assert result.returncode == 0
    settings = Settings(data_dir, "127.0.0.1", 8000)
    engine = create_engine(settings.database_path)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT low_stock_numerator, low_stock_denominator FROM inventory_settings WHERE id=1"
            )
        ).one() == (1, 8)
    engine.dispose()

    invalid = run_cli(
        "init",
        "--data-dir",
        str(data_dir),
        env={"SPOOLTALLY_LOW_STOCK_FRACTION": "1.0000001"},
    )
    assert invalid.returncode == 2
    engine = create_engine(settings.database_path)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT low_stock_numerator, low_stock_denominator FROM inventory_settings WHERE id=1"
            )
        ).one() == (1, 8)
    engine.dispose()


def test_real_cli_init_and_restart_preserve_generation(tmp_path, run_cli):
    data_dir = tmp_path / "cli-init"
    first = run_cli("init", "--data-dir", str(data_dir))
    assert first.returncode == 0
    settings = Settings(data_dir, "127.0.0.1", 8000)
    engine = create_engine(settings.database_path)
    with engine.connect() as connection:
        generation = connection.execute(
            text("SELECT dataset_generation FROM inventory_state WHERE id=1")
        ).scalar_one()
    engine.dispose()
    second = run_cli("init", "--data-dir", str(data_dir))
    assert second.returncode == 0
    engine = create_engine(settings.database_path)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT dataset_generation FROM inventory_state WHERE id=1")
            ).scalar_one()
            == generation
        )
    engine.dispose()


def test_cli_init_works_when_run_outside_checkout(tmp_path):
    import os
    import subprocess
    import sys

    data_dir = tmp_path / "external-cwd"
    result = subprocess.run(
        [sys.executable, "-m", "spooltally", "init", "--data-dir", str(data_dir)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
