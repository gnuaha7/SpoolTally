from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from spooltally.api.app import create_app
from spooltally.config import Settings
from spooltally.db.models import (
    Base,
    CatalogSchema,
    InventoryState,
    TokenRecord,
    TokenScope,
)


def test_schema_list_and_detail_preserve_source_json(tmp_path):
    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app.state.engine)
    now = datetime(2026, 1, 2, tzinfo=UTC)
    raw = b"s" * 32
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with app.state.session_factory() as session:
        auth = TokenRecord(
            id=str(uuid4()),
            name="test",
            token_hash=hashlib.sha256(raw).digest(),
            created_at=now,
        )
        brand = CatalogSchema(
            schema_name="brand",
            source_version="1",
            source_json={"unknown": [0, False, None]},
            fetched_at=now,
        )
        session.add_all(
            [
                auth,
                TokenScope(token=auth, scope="full_access"),
                InventoryState(id=1, dataset_generation=str(uuid4())),
                brand,
            ]
        )
        session.commit()
    expected = {
        "schema_name": "brand",
        "source_version": "1",
        "fetched_at": "2026-01-02T00:00:00Z",
        "source_json": {"unknown": [0, False, None]},
    }
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/catalog/schemas", headers=headers).json() == {
            "items": [expected]
        }
        assert (
            client.get("/api/v1/catalog/schemas/brand", headers=headers).json()
            == expected
        )
        assert client.get(
            "/api/v1/catalog/schemas/missing", headers=headers
        ).json() == {"error": {"code": "not_found", "message": "Resource not found."}}
        assert client.get("/api/v1/catalog/schemas").status_code == 401
    app.state.engine.dispose()
