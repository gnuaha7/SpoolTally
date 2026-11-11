import base64
import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from spooltally.cli import CliError
from spooltally.db.models import Base, TokenRecord, TokenScope
from spooltally.db.session import create_engine, create_session_factory
from spooltally.tokens import create_token, list_tokens, revoke_token


@pytest.fixture
def session_setup(tmp_path: Path):
    engine = create_engine(tmp_path / "tokens.sqlite3")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


def test_create_returns_raw_opaque_token_but_persists_only_hash(session_setup):
    result = create_token(session_setup, "  Printer 🔧  ", "full_access")
    raw = result["token"]
    decoded = base64.urlsafe_b64decode(raw + "=")

    assert set(result) == {"id", "name", "role", "created_at", "token"}
    assert result["name"] == "Printer 🔧"
    assert result["role"] == "full_access"
    assert str(uuid.UUID(result["id"])) == result["id"]
    assert len(raw) == 43 and re.fullmatch(r"[A-Za-z0-9_-]{43}", raw)
    assert len(decoded) == 32
    assert (
        datetime.fromisoformat(result["created_at"].replace("Z", "+00:00"))
        .utcoffset()
        .total_seconds()
        == 0
    )

    with Session(session_setup.kw["bind"]) as session:
        record = session.get(TokenRecord, result["id"])
        assert record is not None
        assert record.token_hash == hashlib.sha256(decoded).digest()
        assert raw.encode() not in record.token_hash
        assert [scope.scope for scope in record.scopes] == ["full_access"]


def test_create_trims_unicode_and_rejects_invalid_name_role_and_duplicate(
    session_setup,
):
    create_token(session_setup, "Name", "full_access")
    assert create_token(session_setup, " name ", "full_access")["name"] == "name"
    assert create_token(session_setup, "x" * 80, "full_access")["name"] == "x" * 80
    for name, role in (
        (" \u2003 ", "full_access"),
        ("x" * 81, "full_access"),
        ("x", "metrics:read"),
    ):
        with pytest.raises(CliError) as error:
            create_token(session_setup, name, role)
        assert (error.value.code, error.value.exit_code) == ("invalid_input", 2)
    with pytest.raises(CliError) as error:
        create_token(session_setup, "Name", "full_access")
    assert (error.value.code, error.value.exit_code) == ("invalid_input", 2)


def test_list_order_retain_revoked_rows_and_never_expose_secrets(session_setup):
    first = create_token(session_setup, "first", "full_access")
    second = create_token(session_setup, "second", "full_access")
    revoke_token(session_setup, first["id"])
    result = list_tokens(session_setup)

    assert result == {
        "tokens": [
            {
                "id": first["id"],
                "name": "first",
                "role": "full_access",
                "created_at": first["created_at"],
                "revoked_at": result["tokens"][0]["revoked_at"],
            },
            {
                "id": second["id"],
                "name": "second",
                "role": "full_access",
                "created_at": second["created_at"],
                "revoked_at": None,
            },
        ]
    }
    assert result["tokens"][0]["revoked_at"] is not None
    assert first["token"] not in json.dumps(result)
    assert second["token"] not in json.dumps(result)


def test_list_orders_whole_seconds_before_later_fractional_timestamp(session_setup):
    with Session(session_setup.kw["bind"]) as session:
        session.add_all(
            [
                TokenRecord(
                    id="00000000-0000-4000-8000-000000000002",
                    name="later fraction",
                    token_hash=b"a" * 32,
                    created_at=datetime.fromisoformat(
                        "2026-11-11T07:00:00.000001+00:00"
                    ),
                ),
                TokenRecord(
                    id="00000000-0000-4000-8000-000000000001",
                    name="exact second",
                    token_hash=b"b" * 32,
                    created_at=datetime.fromisoformat("2026-11-11T07:00:00+00:00"),
                ),
            ]
        )
        session.add_all(
            [
                TokenScope(
                    token_id="00000000-0000-4000-8000-000000000002",
                    scope="full_access",
                ),
                TokenScope(
                    token_id="00000000-0000-4000-8000-000000000001",
                    scope="full_access",
                ),
            ]
        )
        session.commit()

    result = list_tokens(session_setup)
    assert [item["id"] for item in result["tokens"]] == [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    ]


def test_revoke_requires_canonical_uuid_and_is_idempotent(session_setup):
    created = create_token(session_setup, "one", "full_access")
    with pytest.raises(CliError) as error:
        revoke_token(session_setup, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper())
    assert (error.value.code, error.value.exit_code) == ("invalid_input", 2)
    assert revoke_token(session_setup, created["id"]) == {
        "id": created["id"],
        "revoked": True,
        "changed": True,
    }
    assert revoke_token(session_setup, created["id"]) == {
        "id": created["id"],
        "revoked": True,
        "changed": False,
    }
    with pytest.raises(CliError) as error:
        revoke_token(session_setup, str(uuid.uuid4()))
    assert (error.value.code, error.value.exit_code) == ("token_not_found", 3)
    assert created["token"] not in str(error.value)


def test_actual_unmigrated_database_reports_schema_not_ready(tmp_path: Path):
    engine = create_engine(tmp_path / "empty.sqlite3")
    factory = create_session_factory(engine)
    try:
        with pytest.raises(CliError) as error:
            create_token(factory, "one", "full_access")
        assert (error.value.code, error.value.exit_code) == ("schema_not_ready", 2)
    finally:
        engine.dispose()


def test_cli_can_create_token_after_schema_is_initialized(run_cli, tmp_path: Path):
    initialized = run_cli("init", "--data-dir", str(tmp_path))
    assert initialized.returncode == 0
    created = run_cli(
        "tokens", "create", "--name", "printer", "--data-dir", str(tmp_path)
    )
    assert created.returncode == 0
    assert created.stderr == ""
    result = json.loads(created.stdout)
    assert set(result) == {"id", "name", "role", "created_at", "token"}
    assert created.stdout.endswith("\n")
    assert created.stdout.count("\n") == 1
    assert json.dumps(result, separators=(",", ":")) + "\n" == created.stdout
    assert result["created_at"].endswith("Z")

    listed = run_cli("tokens", "list", "--data-dir", str(tmp_path))
    assert listed.returncode == 0
    listed_result = json.loads(listed.stdout)
    assert listed_result == {
        "tokens": [
            {
                "id": result["id"],
                "name": "printer",
                "role": "full_access",
                "created_at": result["created_at"],
                "revoked_at": None,
            }
        ]
    }
    assert result["token"] not in listed.stdout

    revoked = run_cli("tokens", "revoke", result["id"], "--data-dir", str(tmp_path))
    assert revoked.returncode == 0
    assert json.loads(revoked.stdout) == {
        "id": result["id"],
        "revoked": True,
        "changed": True,
    }
    assert result["token"] not in revoked.stdout + revoked.stderr

    invalid = run_cli(
        "tokens",
        "create",
        "--name",
        "printer2",
        "--role",
        "metrics:read",
        "--data-dir",
        str(tmp_path),
    )
    assert invalid.returncode == 2
    assert invalid.stdout == ""
    assert invalid.stderr == (
        '{"error":{"code":"invalid_input","message":"Invalid token input."}}\n'
    )
    assert result["token"] not in invalid.stderr
