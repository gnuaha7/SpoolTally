from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from spooltally.db.models import Base, IdempotencyRecord, TokenRecord
from spooltally.db.session import create_engine, create_session_factory
from spooltally.services.transactions import (
    WriteResult,
    atomic_write,
    check_etag,
    etag,
    fingerprint,
    get_replay,
    parse_if_match,
    save_replay,
    validate_idempotency_key,
)


@pytest.fixture
def database(tmp_path):
    engine = create_engine(tmp_path / "transactions.sqlite")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        session.add(
            TokenRecord(
                id="00000000-0000-0000-0000-000000000001",
                name="test token",
                token_hash=b"t" * 32,
                created_at=datetime.now(UTC),
            )
        )
        session.add(
            TokenRecord(
                id="00000000-0000-0000-0000-000000000002",
                name="second test token",
                token_hash=b"u" * 32,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _hold_immediate_lock(path: str, ready) -> None:
    connection = sqlite3.connect(path, isolation_level=None, timeout=0)
    connection.execute("BEGIN IMMEDIATE")
    ready.set()
    time.sleep(6)
    connection.rollback()
    connection.close()


def test_etag_has_contract_vector_and_rejects_invalid_inputs():
    assert (
        etag(
            "00000000-0000-0000-0000-000000000001",
            "location",
            "00000000-0000-0000-0000-000000000002",
            1,
        )
        == '"st1.bSKR-d0KgIVbIZRF7KOzgmPeG1Ly23bdn8FILYo48tk"'
    )
    with pytest.raises(ValueError):
        etag("gen", "location", "00000000-0000-0000-0000-000000000002", 1)


@pytest.mark.parametrize(
    ("value", "status", "code"),
    [
        (None, 428, "precondition_required"),
        ("*", 400, "invalid_precondition"),
        (
            'W/"st1.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
            400,
            "invalid_precondition",
        ),
        ('"tag1"', 400, "invalid_precondition"),
        (
            '"st1.bSKR-d0KgIVbIZRF7KOzgmPeG1Ly23bdn8FILYo48tk", "other"',
            400,
            "invalid_precondition",
        ),
    ],
)
def test_parse_if_match_rejects_missing_or_malformed(value, status, code):
    with pytest.raises(HTTPException) as error:
        parse_if_match(value)
    assert error.value.status_code == status
    assert error.value.detail["code"] == code


def test_parse_if_match_returns_tag_and_check_etag_detects_stale_value():
    tag = '"st1.bSKR-d0KgIVbIZRF7KOzgmPeG1Ly23bdn8FILYo48tk"'
    assert parse_if_match(tag) == tag
    check_etag(tag, tag)
    with pytest.raises(HTTPException) as error:
        check_etag('"st1.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"', tag)
    assert error.value.status_code == 412
    assert error.value.detail["code"] == "precondition_failed"


@pytest.mark.parametrize(
    "value", [None, "", "a" * 129, "white space", "ümlaut", "\x7f"]
)
def test_idempotency_key_rejects_invalid_values(value):
    if value is None:
        assert validate_idempotency_key(value) is None
    with pytest.raises(HTTPException) as error:
        validate_idempotency_key(value, required=True)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "invalid_input"


def test_idempotency_key_accepts_visible_ascii_boundaries():
    assert validate_idempotency_key("!") == "!"
    assert validate_idempotency_key("~" * 128) == "~" * 128
    with pytest.raises(HTTPException):
        validate_idempotency_key(" ")


def test_fingerprint_uses_canonical_compact_utf8_json():
    actual = fingerprint(
        "patch",
        "/api/v1/locations/{location_id}",
        {"location_id": "abc"},
        {"offset": 0, "limit": 50},
        None,
        {"name": "é", "nested": {"z": 2, "a": 1}},
    )
    canonical = (
        '{"body":{"name":"é","nested":{"a":1,"z":2}},'
        '"if_match":null,"method":"PATCH","path_params":{"location_id":"abc"},'
        '"query":{"limit":50,"offset":0},'
        '"route":"/api/v1/locations/{location_id}"}'
    ).encode()
    assert actual == hashlib.sha256(canonical).digest()
    assert (
        actual.hex()
        == "446bfe5649482d030c2b0994a45988f63915040701604470554f9f2cb907ee1a"
    )


def test_atomic_write_commits_and_rolls_back(database):
    _, factory = database
    with factory() as session:
        with pytest.raises(RuntimeError, match="rollback"):
            with atomic_write(session):
                session.add(
                    IdempotencyRecord(
                        token_id="00000000-0000-0000-0000-000000000001",
                        key="rollback",
                        fingerprint=b"x" * 32,
                        response_status=200,
                        response_body="{}",
                        replay_headers_json={"Content-Type": "application/json"},
                        created_at=datetime.now(UTC),
                        expires_at=datetime.now(UTC) + timedelta(days=7),
                    )
                )
                raise RuntimeError("rollback")
        assert session.execute(select(IdempotencyRecord)).scalar_one_or_none() is None

    with factory() as session:
        with atomic_write(session):
            session.add(
                IdempotencyRecord(
                    token_id="00000000-0000-0000-0000-000000000001",
                    key="committed",
                    fingerprint=b"x" * 32,
                    response_status=200,
                    response_body="{}",
                    replay_headers_json={"Content-Type": "application/json"},
                    created_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                )
            )
    with factory() as session:
        assert (
            session.execute(select(IdempotencyRecord)).scalar_one().key == "committed"
        )


def test_atomic_write_starts_with_begin_immediate(database):
    engine, factory = database
    statements: list[str] = []

    @event.listens_for(engine, "connect")
    def trace_new_connections(dbapi_connection, connection_record):
        dbapi_connection.set_trace_callback(statements.append)

    engine.dispose()
    with factory() as session:
        with atomic_write(session):
            session.execute(select(IdempotencyRecord)).all()
    assert statements[0] == "BEGIN IMMEDIATE"


def test_replay_conflict_token_isolation_and_exact_204_expiry(database):
    _, factory = database
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = WriteResult(204, b"", {})
    with factory() as session:
        with atomic_write(session):
            save_replay(
                session,
                "00000000-0000-0000-0000-000000000001",
                "key",
                b"a" * 32,
                result,
                now=now,
            )
    with factory() as session:
        with atomic_write(session):
            assert (
                get_replay(
                    session,
                    "00000000-0000-0000-0000-000000000001",
                    "key",
                    b"a" * 32,
                    now=now + timedelta(seconds=604799),
                )
                == result
            )
    with factory() as session:
        with atomic_write(session):
            assert (
                get_replay(
                    session,
                    "00000000-0000-0000-0000-000000000002",
                    "key",
                    b"a" * 32,
                    now=now,
                )
                is None
            )
    with factory() as session:
        with atomic_write(session):
            with pytest.raises(HTTPException) as error:
                get_replay(
                    session,
                    "00000000-0000-0000-0000-000000000001",
                    "key",
                    b"b" * 32,
                    now=now,
                )
            assert error.value.status_code == 409
    with factory() as session:
        with atomic_write(session):
            assert (
                get_replay(
                    session,
                    "00000000-0000-0000-0000-000000000001",
                    "key",
                    b"b" * 32,
                    now=now + timedelta(seconds=604800),
                )
                is None
            )
            save_replay(
                session,
                "00000000-0000-0000-0000-000000000001",
                "key",
                b"b" * 32,
                result,
                now=now + timedelta(seconds=604800),
            )
    with factory() as session:
        row = session.execute(select(IdempotencyRecord)).scalar_one()
        assert row.fingerprint == b"b" * 32
        assert row.response_body == ""
        assert row.replay_headers_json == {}


def test_immediate_write_lock_blocks_second_connection_and_maps_busy(database):
    engine, factory = database
    path = str(engine.url.database)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_hold_immediate_lock, args=(path, ready))
    process.start()
    try:
        assert ready.wait(3)
        with factory() as session:
            with pytest.raises(HTTPException) as error:
                with atomic_write(session):
                    pass
        assert error.value.status_code == 503
        assert error.value.detail["code"] == "database_busy"
        assert error.value.headers == {"Retry-After": "1"}
    finally:
        process.join(8)
        if process.is_alive():
            process.terminate()
            process.join()
