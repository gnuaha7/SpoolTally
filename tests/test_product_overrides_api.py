from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from spooltally.api.app import create_app
from spooltally.config import Settings
from spooltally.db.models import (
    Base,
    InventoryState,
    Movement,
    Spool,
    TokenRecord,
    TokenScope,
)


@pytest.fixture
def catalog_client(tmp_path):
    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app.state.engine)
    raw = bytes([13]) * 32
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with app.state.session_factory() as session:
        record = TokenRecord(
            id=str(uuid4()),
            name="test",
            token_hash=hashlib.sha256(raw).digest(),
            created_at=datetime.now(UTC),
        )
        session.add_all(
            (
                record,
                TokenScope(token=record, scope="full_access"),
                InventoryState(id=1, dataset_generation=str(uuid4())),
            )
        )
        session.commit()
    with TestClient(app) as client:
        yield client, {"Authorization": f"Bearer {token}"}, app
    app.state.engine.dispose()


def test_filament_defaults_do_not_rewrite_existing_spools(catalog_client):
    client, auth, app = catalog_client
    manufacturer = client.post(
        "/api/v1/manufacturers",
        json={"name": "Maker"},
        headers=auth | {"Idempotency-Key": "spool-manufacturer"},
    )
    filament = client.post(
        "/api/v1/filaments",
        json={"manufacturer_id": manufacturer.json()["id"], "name": "PLA"},
        headers=auth | {"Idempotency-Key": "spool-filament"},
    )
    filament_id = filament.json()["id"]
    spool_id = str(uuid4())
    now = datetime.now(UTC)
    with app.state.session_factory() as session:
        session.add(
            Spool(
                id=spool_id,
                filament_id=filament_id,
                tare_mg=180_000,
                initial_mg=1_000_000,
                remaining_mg=500_000,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    changed = client.patch(
        f"/api/v1/filaments/{filament_id}",
        json={"default_tare_g": "200", "nominal_weight_g": "750"},
        headers=auth | {"If-Match": filament.headers["etag"]},
    )
    assert changed.status_code == 200, changed.text
    with app.state.session_factory() as session:
        spool = session.scalar(select(Spool).where(Spool.id == spool_id))
        assert spool is not None
        assert (spool.tare_mg, spool.initial_mg, spool.remaining_mg) == (
            180_000,
            1_000_000,
            500_000,
        )
        assert session.scalars(select(Movement)).all() == []
