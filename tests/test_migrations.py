import sqlite3
from pathlib import Path

from sqlalchemy import inspect, text

from spooltally.config import Settings
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
