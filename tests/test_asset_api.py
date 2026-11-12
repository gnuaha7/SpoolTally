from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from spooltally.api.app import create_app
from spooltally.config import Settings
from spooltally.db.models import (
    Asset,
    Base,
    CatalogRecord,
    InventoryState,
    TokenRecord,
    TokenScope,
)


def test_source_removal_retains_local_source_snapshot_and_logo(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app.state.engine)
    raw = bytes([9]) * 32
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    now = datetime.now(UTC)
    with app.state.session_factory() as session:
        record = CatalogRecord(
            id=str(uuid4()),
            kind="brand",
            source_id="brand-1",
            identity_key="brand:brand-1",
            source_baseline_json={"name": "Brand"},
            typed_fields_json={},
            extension_json={},
            raw_json={"name": "Brand"},
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(record)
        session.add(
            Asset(
                id=str(uuid4()),
                source_record=record,
                sha256=hashlib.sha256(b"\x89PNG\r\n\x1a\nlogo").digest(),
                media_type="image/png",
                source_metadata_json={"index": {"id": "logo"}},
                fetched_at=now,
                blob=b"\x89PNG\r\n\x1a\nlogo",
            )
        )
        authrow = TokenRecord(
            id=str(uuid4()),
            name="test",
            token_hash=hashlib.sha256(raw).digest(),
            created_at=now,
        )
        session.add(authrow)
        session.add(TokenScope(token=authrow, scope="full_access"))
        session.add(InventoryState(id=1, dataset_generation=str(uuid4())))
        session.commit()
        asset_id = session.query(Asset).one().id
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/assets/{asset_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.content == b"\x89PNG\r\n\x1a\nlogo"
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-length"] == str(len(response.content))
        assert client.get(f"/api/v1/assets/{asset_id}").status_code == 401
    app.state.engine.dispose()
