from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, event
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker


def create_engine(database_path: Path) -> Engine:
    engine = sqlalchemy_create_engine(URL.create("sqlite", database=str(database_path)))

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, connection_record) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def begin_transaction(connection) -> None:
        mode = connection.get_execution_options().get("sqlite_begin_mode", "DEFERRED")
        if mode not in {"DEFERRED", "IMMEDIATE"}:
            raise ValueError("Invalid SQLite transaction mode.")
        connection.exec_driver_sql(f"BEGIN {mode}")

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
