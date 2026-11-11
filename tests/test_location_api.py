from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spooltally.api.app import create_app
from spooltally.config import Settings
from spooltally.db.models import (
    Base,
    IdempotencyRecord,
    InventoryState,
    LocalFilament,
    LocalManufacturer,
    Location,
    Movement,
    Spool,
    TokenRecord,
    TokenScope,
)


@pytest.fixture
def location_client(tmp_path: Path):
    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app.state.engine)
    raw = bytes([9]) * 32
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with app.state.session_factory() as session:
        record = TokenRecord(
            id=str(uuid4()),
            name="test",
            token_hash=hashlib.sha256(raw).digest(),
            created_at=datetime.now(UTC),
        )
        session.add(record)
        session.add(TokenScope(token=record, scope="full_access"))
        session.add(InventoryState(id=1, dataset_generation=str(uuid4())))
        session.commit()
        token_id = record.id
    with TestClient(app) as client:
        yield client, {"Authorization": f"Bearer {token}"}, app, token_id
    app.state.engine.dispose()


def test_location_crud_pilot_honors_idempotency_and_revision(location_client) -> None:
    client, auth, app, _ = location_client
    headers = auth | {"Idempotency-Key": "create-A"}
    created = client.post(
        "/api/v1/locations", json={"name": " Shelf A "}, headers=headers
    )
    replay = client.post("/api/v1/locations", json={"name": "Shelf A"}, headers=headers)
    assert created.status_code == replay.status_code == 201
    assert (
        created.json()
        == replay.json()
        == {"id": created.json()["id"], "name": "Shelf A"}
    )
    assert created.headers["etag"] == replay.headers["etag"]
    assert created.headers["location"] == f"/api/v1/locations/{created.json()['id']}"
    location_id = created.json()["id"]
    fetched = client.get(f"/api/v1/locations/{location_id}", headers=auth)
    renamed = client.patch(
        f"/api/v1/locations/{location_id}",
        json={"name": "Shelf B"},
        headers=auth | {"If-Match": fetched.headers["etag"]},
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "Shelf B"
    with app.state.session_factory() as session:
        persisted = session.get(Location, location_id)
        assert persisted is not None and persisted.revision == 2
    assert renamed.headers["etag"] != fetched.headers["etag"]
    stale = client.patch(
        f"/api/v1/locations/{location_id}",
        json={"name": "Shelf C"},
        headers=auth | {"If-Match": fetched.headers["etag"]},
    )
    assert stale.status_code == 412
    deleted = client.delete(
        f"/api/v1/locations/{location_id}",
        headers=auth
        | {"If-Match": renamed.headers["etag"], "Idempotency-Key": "del-A"},
    )
    deleted_replay = client.delete(
        f"/api/v1/locations/{location_id}",
        headers=auth
        | {"If-Match": renamed.headers["etag"], "Idempotency-Key": "del-A"},
    )
    assert deleted.status_code == deleted_replay.status_code == 204
    assert deleted.content == deleted_replay.content == b""
    for response in (deleted, deleted_replay):
        assert all(
            h not in response.headers for h in ("content-type", "etag", "location")
        )
    assert (
        client.get(f"/api/v1/locations/{location_id}", headers=auth).status_code == 404
    )
    with app.state.session_factory() as session:
        assert session.scalar(select(Movement)) is None
    with app.state.session_factory() as session:
        assert session.scalar(select(Location)) is None
        assert session.scalar(select(Movement)) is None


def test_location_responses_follow_etag_header_contract(location_client) -> None:
    client, auth, _, _ = location_client
    collection = client.get("/api/v1/locations", headers=auth)
    assert collection.json() == {"items": [], "limit": 50, "offset": 0, "total": 0}
    assert "etag" not in collection.headers
    created = client.post(
        "/api/v1/locations",
        json={"name": "Étagère"},
        headers=auth | {"Idempotency-Key": "etag"},
    )
    assert created.status_code == 201 and created.headers["etag"].startswith('"st1.')
    assert created.headers["content-type"] == "application/json"
    read = client.get(f"/api/v1/locations/{created.json()['id']}", headers=auth)
    assert read.headers["etag"] == created.headers["etag"]
    assert "location" not in read.headers


@pytest.mark.parametrize(
    "raw",
    [b'{"name":"A","name":"B"}', b'{"name":NaN}', b'{"name":"A","extra":1}', b"[]"],
)
def test_create_rejects_invalid_json_shapes_before_writing(
    location_client, raw: bytes
) -> None:
    client, auth, app, _ = location_client
    response = client.post(
        "/api/v1/locations",
        content=raw,
        headers=auth | {"Content-Type": "application/json", "Idempotency-Key": "bad"},
    )
    assert response.status_code == 422
    with app.state.session_factory() as session:
        assert session.scalar(select(Location)) is None


def test_pagination_requires_ascii_decimal_and_sorts_names(location_client) -> None:
    client, auth, _, _ = location_client
    for name in ("z", "a", "a"):
        client.post(
            "/api/v1/locations",
            json={"name": name},
            headers=auth | {"Idempotency-Key": name + str(uuid4())},
        )
    assert [
        x["name"]
        for x in client.get("/api/v1/locations?limit=2&offset=1", headers=auth).json()[
            "items"
        ]
    ] == ["a", "z"]
    for query in (
        "limit=+1",
        "limit=0",
        "limit=201",
        "offset=-1",
        "offset=9223372036854775808",
        "limit=١",
    ):
        assert client.get(f"/api/v1/locations?{query}", headers=auth).status_code == 422
    too_many_digits = "9" * 4301
    for parameter in ("limit", "offset"):
        response = client.get(
            f"/api/v1/locations?{parameter}={too_many_digits}", headers=auth
        )
        assert response.status_code == 422


def test_preconditions_unknown_id_and_auth(location_client) -> None:
    client, auth, _, _ = location_client
    missing = str(uuid4())
    assert client.get(f"/api/v1/locations/{missing}", headers=auth).status_code == 404
    assert (
        client.patch(
            f"/api/v1/locations/{missing}", json={"name": "x"}, headers=auth
        ).status_code
        == 428
    )
    created = client.post(
        "/api/v1/locations",
        json={"name": "A"},
        headers=auth | {"Idempotency-Key": "precondition"},
    )
    location_id = created.json()["id"]
    for invalid in ("*", 'W/"x"', '"a", "b"', "x"):
        assert (
            client.patch(
                f"/api/v1/locations/{location_id}",
                json={"name": "B"},
                headers=auth | {"If-Match": invalid},
            ).status_code
            == 400
        )
    assert client.get("/api/v1/locations").status_code == 401


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize("different_tags", [False, True])
def test_duplicate_if_match_fields_are_invalid_preconditions_without_writes(
    location_client, method: str, different_tags: bool
) -> None:
    client, auth, app, _ = location_client
    created = client.post(
        "/api/v1/locations",
        json={"name": "Before"},
        headers=auth | {"Idempotency-Key": "duplicate-header-create"},
    )
    location_id = created.json()["id"]
    current_tag = created.headers["etag"]
    other_tag = '"st1.' + "A" * 43 + '"' if different_tags else current_tag
    duplicate_headers = list(auth.items()) + [
        ("If-Match", current_tag),
        ("If-Match", other_tag),
        ("Idempotency-Key", f"duplicate-{method}-{different_tags}"),
    ]

    response = client.request(
        method,
        f"/api/v1/locations/{location_id}",
        headers=duplicate_headers,
        json={"name": "After"} if method == "PATCH" else None,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_precondition"
    assert client.get(f"/api/v1/locations/{location_id}", headers=auth).json() == {
        "id": location_id,
        "name": "Before",
    }
    with app.state.session_factory() as session:
        location = session.get(Location, location_id)
        assert location is not None and location.revision == 1
        assert session.scalar(select(Movement)) is None
        records = session.scalars(select(IdempotencyRecord)).all()
        assert [record.key for record in records] == ["duplicate-header-create"]


def test_idempotency_conflict_and_token_scope(location_client) -> None:
    client, auth, app, _ = location_client
    first = client.post(
        "/api/v1/locations",
        json={"name": "A"},
        headers=auth | {"Idempotency-Key": "same"},
    )
    conflict = client.post(
        "/api/v1/locations",
        json={"name": "B"},
        headers=auth | {"Idempotency-Key": "same"},
    )
    assert first.status_code == 201 and conflict.status_code == 409
    with app.state.session_factory() as session:
        assert len(session.scalars(select(Location)).all()) == 1


def test_idempotency_is_isolated_by_token(location_client) -> None:
    client, auth, app, _ = location_client
    raw = bytes([10]) * 32
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with app.state.session_factory() as session:
        record = TokenRecord(
            id=str(uuid4()),
            name="second",
            token_hash=hashlib.sha256(raw).digest(),
            created_at=datetime.now(UTC),
        )
        session.add(record)
        session.add(TokenScope(token=record, scope="full_access"))
        session.commit()
    first = client.post(
        "/api/v1/locations",
        json={"name": "A"},
        headers=auth | {"Idempotency-Key": "shared-key"},
    )
    second = client.post(
        "/api/v1/locations",
        json={"name": "A"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "shared-key",
        },
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_create_requires_idempotency_key(location_client) -> None:
    client, auth, app, _ = location_client
    response = client.post("/api/v1/locations", json={"name": "A"}, headers=auth)
    assert response.status_code == 422
    with app.state.session_factory() as session:
        assert session.scalar(select(Location)) is None


def test_location_noop_preserves_etag(location_client) -> None:
    client, auth, _, _ = location_client
    created = client.post(
        "/api/v1/locations",
        json={"name": "A"},
        headers=auth | {"Idempotency-Key": "noop"},
    )
    location_id = created.json()["id"]
    tag = created.headers["etag"]
    no_op = client.patch(
        f"/api/v1/locations/{location_id}",
        json={"name": " A "},
        headers=auth | {"If-Match": tag},
    )
    assert no_op.status_code == 200 and no_op.headers["etag"] == tag


def test_referenced_location_cannot_be_deleted(location_client) -> None:
    client, auth, app, _ = location_client
    created = client.post(
        "/api/v1/locations",
        json={"name": "In use"},
        headers=auth | {"Idempotency-Key": "in-use-create"},
    )
    location_id = created.json()["id"]
    now = datetime.now(UTC)
    manufacturer_id = str(uuid4())
    filament_id = str(uuid4())
    with app.state.session_factory() as session:
        manufacturer = LocalManufacturer(
            id=manufacturer_id,
            name="Maker",
            created_at=now,
            updated_at=now,
        )
        filament = LocalFilament(
            id=filament_id,
            manufacturer=manufacturer,
            name="PLA",
            created_at=now,
            updated_at=now,
        )
        session.add_all((manufacturer, filament))
        session.flush()
        session.add(
            Spool(
                id=str(uuid4()),
                filament=filament,
                location_id=location_id,
                initial_mg=1000,
                remaining_mg=1000,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    rejected = client.delete(
        f"/api/v1/locations/{location_id}",
        headers=auth
        | {"If-Match": created.headers["etag"], "Idempotency-Key": "in-use-delete"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "location_in_use"
    assert (
        client.get(f"/api/v1/locations/{location_id}", headers=auth).status_code == 200
    )


def test_authentication_precedes_location_body_effects(location_client) -> None:
    client, _, app, _ = location_client
    response = client.post(
        "/api/v1/locations",
        content=b'{"name":"A"}',
        headers={"Content-Type": "application/json", "Idempotency-Key": "unauth"},
    )
    assert response.status_code == 401
    with app.state.session_factory() as session:
        assert session.scalar(select(Location)) is None


def test_cli_token_and_launched_server_preserve_location_etag_on_restart(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "src")
    data_dir = tmp_path / "data"
    initialized = subprocess.run(
        [sys.executable, "-m", "spooltally", "init", "--data-dir", str(data_dir)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    issued = subprocess.run(
        [
            sys.executable,
            "-m",
            "spooltally",
            "tokens",
            "create",
            "--name",
            "location acceptance",
            "--data-dir",
            str(data_dir),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert issued.returncode == 0, issued.stderr
    token = json.loads(issued.stdout)["token"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [
        sys.executable,
        "-m",
        "spooltally",
        "serve",
        "--data-dir",
        str(data_dir),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]

    def start_server() -> subprocess.Popen[str]:
        process = subprocess.Popen(
            command,
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError("server exited before becoming ready")
            try:
                httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.2)
                return process
            except httpx.HTTPError:
                time.sleep(0.05)
        process.terminate()
        raise AssertionError("server did not become ready")

    def stop_server(process: subprocess.Popen[str]) -> None:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        assert token not in stdout + stderr

    process = start_server()
    base_url = f"http://127.0.0.1:{port}/api/v1/locations"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        created = httpx.post(
            base_url,
            json={"name": "Restart shelf"},
            headers=headers | {"Idempotency-Key": "restart-create"},
            timeout=3,
        )
        assert created.status_code == 201
        location_id = created.json()["id"]
        first_tag = created.headers["etag"]
    finally:
        stop_server(process)
    process = start_server()
    try:
        read = httpx.get(f"{base_url}/{location_id}", headers=headers, timeout=3)
        assert read.status_code == 200 and read.headers["etag"] == first_tag
    finally:
        stop_server(process)
