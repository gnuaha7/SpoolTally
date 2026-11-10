from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alembic.config import Config
from sqlalchemy import Connection, Engine, inspect, text

from alembic import command
from spooltally.cli import CliError
from spooltally.config import Settings
from spooltally.db.session import create_engine

MODES = {"normal", "restore"}


def set_restore_mode(connection: Connection, mode: str) -> None:
    if mode not in MODES:
        raise ValueError("Invalid restore mode.")
    if not connection.in_transaction():
        raise RuntimeError("Restore mode requires an active transaction.")
    connection.execute(
        text("UPDATE restore_control SET mode=:mode WHERE id=1"), {"mode": mode}
    )


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _verify(path: Path) -> None:
    with sqlite3.connect(path) as db:
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Recovery backup integrity check failed.")


def _backup(
    settings: Settings, destination: Path, locked_connection: Connection
) -> Path:
    if not locked_connection.in_transaction():
        raise RuntimeError("Recovery backup requires an active transaction.")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    (settings.data_dir / "backups").chmod(0o700)
    destination.parent.chmod(0o700)
    source_path = settings.database_path
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        target.commit()
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        target.close()
        source.close()
    destination.chmod(0o600)
    _verify(destination)
    return destination


def backup_before_migration(
    settings: Settings, target_revision: str, locked_connection: Connection
) -> Path | None:
    if not locked_connection.in_transaction():
        raise RuntimeError("Migration backup requires an active transaction.")
    existing = set(inspect(locked_connection).get_table_names()) - {"alembic_version"}
    if not existing:
        return None
    tables = set(inspect(locked_connection).get_table_names())
    if "alembic_version" in tables:
        current = locked_connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if current == target_revision:
            return None
    destination = (
        settings.data_dir
        / "backups"
        / "pre-migration"
        / f"{target_revision}-{_timestamp()}-{uuid.uuid4()}.sqlite3"
    )
    return _backup(settings, destination, locked_connection)


def create_private_recovery_backup(
    settings: Settings, operation_id: str, locked_connection: Connection
) -> Path:
    try:
        operation_id = str(uuid.UUID(operation_id))
    except ValueError as exc:
        raise ValueError("Invalid operation UUID.") from exc
    destination = (
        settings.data_dir
        / "backups"
        / "pre-restore"
        / f"{operation_id}-{_timestamp()}.sqlite3"
    )
    return _backup(settings, destination, locked_connection)


def prune_recovery_backups(directory: Path, keep: int = 5) -> None:
    pattern = re.compile(r"\d{8}T\d{6}\.\d{6}Z")

    def timestamp(path: Path) -> str:
        match = pattern.search(path.name)
        return "" if match is None else match.group()

    backups = sorted(
        directory.glob("*.sqlite3"),
        key=timestamp,
        reverse=True,
    )
    verified = []
    for path in backups:
        try:
            _verify(path)
        except (OSError, sqlite3.DatabaseError, RuntimeError):
            continue
        verified.append(path)
    for path in verified[keep:]:
        path.unlink()


def _fraction() -> tuple[int, int] | None:
    raw = os.environ.get("SPOOLTALLY_LOW_STOCK_FRACTION")
    if raw is None:
        return None
    if not re.fullmatch(r"(?:0|1)(?:\.\d{1,6})?", raw):
        raise CliError("invalid_input", "Invalid input.", 2)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise CliError("invalid_input", "Invalid input.", 2) from exc
    if not Decimal(0) < value <= Decimal(1):
        raise CliError("invalid_input", "Invalid input.", 2)
    scale = 10 ** max(0, -int(value.as_tuple().exponent))
    numerator = int(value * scale)
    from math import gcd

    divisor = gcd(numerator, scale)
    return numerator // divisor, scale // divisor


def _timezone_name() -> str:
    name = os.environ.get("TZ")
    if name and name.startswith(":"):
        name = name[1:]
    if not name:
        try:
            name = Path("/etc/localtime").resolve().as_posix().split("/zoneinfo/", 1)[1]
        except (OSError, IndexError):
            raise CliError("invalid_input", "Invalid machine timezone.", 2) from None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CliError("invalid_input", "Invalid machine timezone.", 2) from exc
    return name


def _prune_after_commit(directory: Path) -> None:
    prune_recovery_backups(directory)


def initialize_database(settings: Settings) -> None:
    fraction = _fraction()
    timezone_name = _timezone_name()
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    engine: Engine = create_engine(settings.database_path)
    recovery: Path | None = None
    try:
        with engine.connect().execution_options(
            sqlite_begin_mode="IMMEDIATE"
        ) as connection:
            transaction = connection.begin()
            try:
                cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
                cfg.attributes["connection"] = connection
                script = __import__(
                    "alembic.script", fromlist=["ScriptDirectory"]
                ).ScriptDirectory.from_config(cfg)
                target = script.get_current_head() or "0001_initial"
                recovery = backup_before_migration(settings, target, connection)
                command.upgrade(cfg, "head")
                connection.execute(
                    text(
                        "INSERT OR IGNORE INTO inventory_state "
                        "(id, dataset_generation) VALUES (1, :generation)"
                    ),
                    {"generation": str(uuid.uuid4())},
                )
                connection.execute(
                    text(
                        "INSERT OR IGNORE INTO restore_control "
                        "(id, mode) VALUES (1, 'normal')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT OR IGNORE INTO inventory_settings "
                        "(id, low_stock_numerator, low_stock_denominator) "
                        "VALUES (1, 1, 10)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT OR IGNORE INTO catalog_schedule_state "
                        "(id, last_handled_slot_key, timezone_name, wall_time) "
                        "VALUES (1, NULL, :zone, '03:00')"
                    ),
                    {"zone": timezone_name},
                )
                connection.execute(
                    text(
                        "UPDATE catalog_schedule_state SET timezone_name=:zone "
                        "WHERE id=1"
                    ),
                    {"zone": timezone_name},
                )
                if fraction is not None:
                    connection.execute(
                        text(
                            "UPDATE inventory_settings SET low_stock_numerator=:n, "
                            "low_stock_denominator=:d WHERE id=1"
                        ),
                        {"n": fraction[0], "d": fraction[1]},
                    )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
        if recovery is not None:
            _prune_after_commit(recovery.parent)
    finally:
        engine.dispose()
