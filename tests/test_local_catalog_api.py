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
    CatalogRecord,
    InventoryState,
    LocalFilament,
    LocalManufacturer,
    SourceFieldOverride,
    TokenRecord,
    TokenScope,
)
from spooltally.inventory.catalog import (
    apply_overrides,
    grams_to_mg,
    parse_pointer,
    validate_new_pointer,
)


@pytest.fixture
def catalog_client(tmp_path):
    app = create_app(Settings(tmp_path, "127.0.0.1", 0))
    Base.metadata.create_all(app.state.engine)
    raw = bytes([12]) * 32
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    now = datetime.now(UTC)
    with app.state.session_factory() as session:
        record = TokenRecord(
            id=str(uuid4()),
            name="test",
            token_hash=hashlib.sha256(raw).digest(),
            created_at=now,
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


def test_pointers_decode_escaped_and_numeric_object_members():
    assert parse_pointer("/a~1b/0/~0") == ("a/b", "0", "~")
    assert apply_overrides({"0": {"x": 1}}, {"/0/x": None}) == {"0": {"x": None}}


def test_override_can_add_final_object_member_and_arrays_are_atomic():
    assert apply_overrides({"a": {}}, {"/a/new": 0}) == {"a": {"new": 0}}
    assert apply_overrides({"a": [1]}, {"/a": [2, 3]}) == {"a": [2, 3]}
    with pytest.raises(ValueError):
        validate_new_pointer({"a": [1]}, "/a/0")


def test_existing_override_survives_source_parent_shape_change():
    assert apply_overrides({"a": 4}, {"/a/b": False}) == {"a": {"b": False}}
    assert apply_overrides({}, {"/a/b": None}) == {"a": {"b": None}}
    assert apply_overrides({"a": [4]}, {"/a/0": 2}) == {"a": {"0": 2}}


def test_gram_conversion_is_exact_and_bounded():
    assert grams_to_mg("9223372036854.775") == 9223372036854775
    assert grams_to_mg("9223372036854775.807") == 2**63 - 1
    assert grams_to_mg("0.001") == 1
    for bad in ("-1", "+1", "1e2", " 1", "1.0001", "9223372036854775.808"):
        with pytest.raises(ValueError):
            grams_to_mg(bad)


def test_local_filament_setup_override_effective_and_null_replay(catalog_client):
    client, auth, app = catalog_client
    now = datetime.now(UTC)
    source_id = str(uuid4())
    with app.state.session_factory() as session:
        session.add(
            CatalogRecord(
                id=source_id,
                kind="filament",
                source_id="raw",
                identity_key="filament:raw",
                source_baseline_json={"brand": {"name": "Maker"}, "color": "blue"},
                typed_fields_json={},
                extension_json={},
                raw_json={},
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        session.commit()
    manufacturer = client.post(
        "/api/v1/manufacturers",
        json={"name": "Maker"},
        headers=auth | {"Idempotency-Key": "m"},
    )
    assert manufacturer.status_code == 201, manufacturer.text
    created = client.post(
        "/api/v1/filaments",
        json={
            "manufacturer_id": manufacturer.json()["id"],
            "source_filament_id": source_id,
            "name": "PLA",
            "default_tare_g": "180",
            "nominal_weight_g": "1000",
        },
        headers=auth | {"Idempotency-Key": "f"},
    )
    assert created.status_code == 201, created.text
    fid = created.json()["id"]
    first = client.patch(
        f"/api/v1/filaments/{fid}/overrides",
        json={"set": {"/brand/color": None}},
        headers=auth | {"If-Match": created.headers["etag"], "Idempotency-Key": "null"},
    )
    replay = client.patch(
        f"/api/v1/filaments/{fid}/overrides",
        json={"set": {"/brand/color": None}},
        headers=auth | {"If-Match": created.headers["etag"], "Idempotency-Key": "null"},
    )
    assert first.status_code == 200 and replay.content == first.content
    assert first.json()["overrides"] == {"/brand/color": None}
    effective = client.get(f"/api/v1/filaments/{fid}/effective", headers=auth)
    assert effective.json()["effective_source_json"] == {
        "brand": {"name": "Maker", "color": None},
        "color": "blue",
    }
    assert "etag" not in effective.headers
    with app.state.session_factory() as session:
        source = session.get(CatalogRecord, source_id)
        assert source is not None
        source.source_baseline_json = {"brand": "Updated", "color": "red"}
        session.commit()
    read = client.get(f"/api/v1/filaments/{fid}", headers=auth)
    assert read.headers["etag"] == first.headers["etag"]
    changed_source = client.get(f"/api/v1/filaments/{fid}/effective", headers=auth)
    assert changed_source.json()["effective_source_json"] == {
        "brand": {"color": None},
        "color": "red",
    }
    reset = client.patch(
        f"/api/v1/filaments/{fid}/overrides",
        json={"reset": ["/brand/color"]},
        headers=auth | {"If-Match": first.headers["etag"]},
    )
    assert reset.status_code == 200
    assert client.get(f"/api/v1/filaments/{fid}/effective", headers=auth).json()[
        "effective_source_json"
    ] == {"brand": "Updated", "color": "red"}


def test_local_filament_prerequisite_for_two_spools(catalog_client):
    client, auth, _ = catalog_client
    maker = client.post(
        "/api/v1/manufacturers",
        json={"name": "Local maker", "notes": "kept local"},
        headers=auth | {"Idempotency-Key": "prerequisite-maker"},
    )
    filament = client.post(
        "/api/v1/filaments",
        json={"manufacturer_id": maker.json()["id"], "name": "PLA"},
        headers=auth | {"Idempotency-Key": "prerequisite-filament"},
    )
    assert maker.status_code == filament.status_code == 201
    assert filament.json()["manufacturer_id"] == maker.json()["id"]
    assert filament.json()["default_tare_g"] is None
    assert filament.json()["nominal_weight_g"] is None


def test_invalid_pointer_batches_and_json_do_not_write(catalog_client):
    client, auth, app = catalog_client
    maker = client.post(
        "/api/v1/manufacturers",
        json={"name": "Maker"},
        headers=auth | {"Idempotency-Key": "invalid-maker"},
    )
    filament = client.post(
        "/api/v1/filaments",
        json={"manufacturer_id": maker.json()["id"], "name": "PLA"},
        headers=auth | {"Idempotency-Key": "invalid-filament"},
    )
    fid, tag = filament.json()["id"], filament.headers["etag"]
    invalid_weight = client.post(
        "/api/v1/filaments",
        json={
            "manufacturer_id": maker.json()["id"],
            "name": "Bad",
            "default_tare_g": "1e2",
        },
        headers=auth | {"Idempotency-Key": "bad-weight"},
    )
    assert invalid_weight.status_code == 422
    for body in (
        {"set": {"/missing/child": 1}},
        {"set": {"/a": 1, "/a/b": 2}},
        {"set": {"/bad~2escape": 1}},
        {"set": {"/": 1}, "reset": ["/"]},
    ):
        response = client.patch(
            f"/api/v1/filaments/{fid}/overrides",
            json=body,
            headers=auth | {"If-Match": tag},
        )
        assert response.status_code == 422
    raw = client.post(
        "/api/v1/manufacturers",
        content=b'{"name":"A","name":"B"}',
        headers=auth | {"Idempotency-Key": "duplicate-json"},
    )
    assert raw.status_code == 422
    with app.state.session_factory() as session:
        persisted = session.get(LocalFilament, fid)
        assert persisted is not None and persisted.revision == 1
        assert (
            session.scalars(
                select(SourceFieldOverride).where(
                    SourceFieldOverride.local_filament_id == fid
                )
            ).all()
            == []
        )
        assert (
            session.scalar(
                select(LocalManufacturer).where(LocalManufacturer.name == "B")
            )
            is None
        )


def test_persisted_override_state_controls_noop_and_type_aware_values(catalog_client):
    client, auth, app = catalog_client
    manufacturer = client.post(
        "/api/v1/manufacturers",
        json={"name": "M"},
        headers=auth | {"Idempotency-Key": "m2"},
    )
    filament = client.post(
        "/api/v1/filaments",
        json={"manufacturer_id": manufacturer.json()["id"], "name": "F"},
        headers=auth | {"Idempotency-Key": "f2"},
    )
    fid, tag = filament.json()["id"], filament.headers["etag"]
    added = client.patch(
        f"/api/v1/filaments/{fid}/overrides",
        json={"set": {"/x": 1}},
        headers=auth | {"If-Match": tag},
    )
    assert (
        added.headers["etag"] != tag
    )  # new persisted row changes even on empty baseline
    same = client.patch(
        f"/api/v1/filaments/{fid}/overrides",
        json={"set": {"/x": 1}},
        headers=auth | {"If-Match": added.headers["etag"]},
    )
    assert same.headers["etag"] == added.headers["etag"]
    typed = client.patch(
        f"/api/v1/filaments/{fid}/overrides",
        json={"set": {"/x": True}},
        headers=auth | {"If-Match": same.headers["etag"]},
    )
    assert typed.headers["etag"] != same.headers["etag"]


def test_gram_spellings_share_normalized_idempotency_fingerprint(catalog_client):
    client, auth, _ = catalog_client
    maker = client.post(
        "/api/v1/manufacturers",
        json={"name": "Maker"},
        headers=auth | {"Idempotency-Key": "normalized-maker"},
    )
    filament = client.post(
        "/api/v1/filaments",
        json={"manufacturer_id": maker.json()["id"], "name": "PLA"},
        headers=auth | {"Idempotency-Key": "normalized-filament"},
    )
    path = f"/api/v1/filaments/{filament.json()['id']}"
    headers = auth | {
        "If-Match": filament.headers["etag"],
        "Idempotency-Key": "weight",
    }
    first = client.patch(path, json={"default_tare_g": "1.0"}, headers=headers)
    replay = client.patch(path, json={"default_tare_g": "1.000"}, headers=headers)
    assert first.status_code == 200 and replay.content == first.content
    assert first.json()["default_tare_g"] == "1"


def test_source_kinds_and_variant_parentage_are_checked(catalog_client):
    client, auth, app = catalog_client
    now = datetime.now(UTC)
    source_id, other_id, variant_id = str(uuid4()), str(uuid4()), str(uuid4())
    with app.state.session_factory() as session:
        session.add_all(
            [
                CatalogRecord(
                    id=source_id,
                    kind="filament",
                    source_id="f1",
                    identity_key="f1",
                    source_baseline_json={"color": "blue"},
                    typed_fields_json={},
                    extension_json={},
                    raw_json={},
                    first_seen_at=now,
                    last_seen_at=now,
                ),
                CatalogRecord(
                    id=other_id,
                    kind="filament",
                    source_id="f2",
                    identity_key="f2",
                    source_baseline_json={"color": "red"},
                    typed_fields_json={},
                    extension_json={},
                    raw_json={},
                    first_seen_at=now,
                    last_seen_at=now,
                ),
            ]
        )
        session.flush()
        session.add(
            CatalogRecord(
                id=variant_id,
                kind="variant",
                source_id="v1",
                identity_key="v1",
                parent_id=other_id,
                source_baseline_json={"size": "1.75"},
                typed_fields_json={},
                extension_json={},
                raw_json={},
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        session.commit()
    wrong_brand = client.post(
        "/api/v1/manufacturers",
        json={"name": "Wrong", "source_brand_id": source_id},
        headers=auth | {"Idempotency-Key": "wrong-brand-kind"},
    )
    assert wrong_brand.status_code == 422
    maker = client.post(
        "/api/v1/manufacturers",
        json={"name": "Maker"},
        headers=auth | {"Idempotency-Key": "source-maker"},
    )
    rejected = client.post(
        "/api/v1/filaments",
        json={
            "manufacturer_id": maker.json()["id"],
            "source_filament_id": source_id,
            "selected_variant_id": variant_id,
            "name": "PLA",
        },
        headers=auth | {"Idempotency-Key": "wrong-variant-parent"},
    )
    assert rejected.status_code == 422
    accepted = client.post(
        "/api/v1/filaments",
        json={
            "manufacturer_id": maker.json()["id"],
            "source_filament_id": other_id,
            "selected_variant_id": variant_id,
            "name": "PLA",
        },
        headers=auth | {"Idempotency-Key": "right-variant-parent"},
    )
    assert accepted.status_code == 201
    projection = client.get(
        f"/api/v1/filaments/{accepted.json()['id']}/effective", headers=auth
    )
    assert projection.json()["effective_source_json"] == {"color": "red"}
    assert projection.json()["selected_variant_source_json"] == {"size": "1.75"}
