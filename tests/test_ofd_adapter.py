import json
from pathlib import Path

import pytest

from spooltally.ofd.adapter import KINDS, AdapterError, adapt_catalog
from spooltally.ofd.schemas import SOURCE_SCHEMA_NAMES

FIXTURES = Path(__file__).parent / "fixtures" / "ofd"


def inputs():
    schemas = {
        p.stem: p.read_bytes()
        for p in FIXTURES.glob("*.json")
        if p.name not in {"bulk.json", "fixture_manifest.json"}
    }
    return schemas


def minimal():
    return {
        "version": "1",
        "generated_at": "now",
        **{
            k: []
            for k in (
                "brands",
                "materials",
                "filaments",
                "variants",
                "sizes",
                "stores",
                "purchase_links",
            )
        },
    }


def test_round_trip_raw_catalog_and_unknowns():
    doc = minimal()
    doc["brands"] = [
        {
            "id": "brand",
            "name": "A",
            "logo": "",
            "website": "",
            "origin": "",
            "new": {"x": [False, None]},
        }
    ]
    result = adapt_catalog(json.dumps(doc), inputs())
    assert result.raw == doc
    assert result.records[0].raw_json == doc["brands"][0]
    assert result.records[0].extension_json["new"] == {"x": [False, None]}


def test_rename_keeps_uuid_identity():
    doc = minimal()
    doc["brands"] = [
        {
            "id": "brand",
            "uuid": "12345678-1234-5678-1234-567812345678",
            "name": "A",
            "logo": "",
            "website": "",
            "origin": "",
        }
    ]
    one = adapt_catalog(json.dumps(doc), inputs()).records[0]
    doc["brands"][0]["name"] = "B"
    two = adapt_catalog(json.dumps(doc), inputs()).records[0]
    assert (
        one.identity_key
        == two.identity_key
        == "uuid:12345678-1234-5678-1234-567812345678"
    )


def test_baseline_preserves_null_zero_false_absence():
    doc = minimal()
    doc["brands"] = [{"id": "b", "name": "B", "logo": "", "website": "", "origin": ""}]
    doc["materials"] = [{"id": "m", "brand_id": "b", "material": "PLA"}]
    doc["filaments"] = [
        {
            "id": "f",
            "material_id": "m",
            "name": "F",
            "density": 1.2,
            "diameter_tolerance": 0.1,
        }
    ]
    doc["variants"] = [
        {
            "id": "v",
            "filament_id": "f",
            "name": "n",
            "color_hex": ["#fff"],
            "discontinued": False,
            "transmission_distance": 0,
        }
    ]
    result = adapt_catalog(json.dumps(doc), inputs())
    raw = next(r for r in result.records if r.kind == "variant").source_baseline_json
    assert raw["discontinued"] is False and raw["transmission_distance"] == 0
    assert "uuid" not in raw and "hex_variants" not in raw


def test_corrupt_payload_is_rejected_without_partial_result():
    doc = minimal()
    doc["brands"] = [
        {
            "id": "x",
            "name": "A",
            "logo": "",
            "website": "",
            "origin": "",
            "discontinued": 1,
        }
    ]
    with pytest.raises(AdapterError):
        adapt_catalog(json.dumps(doc), inputs())


def test_schema_names_are_complete_and_preserved():
    assert set(inputs()) == set(SOURCE_SCHEMA_NAMES)


def test_all_collections_resolve_hierarchy_and_scalar_links():
    doc = minimal()
    doc["brands"] = [{"id": "b", "name": "B", "logo": "", "website": "", "origin": ""}]
    doc["materials"] = [{"id": "m", "brand_id": "b", "material": "PLA"}]
    doc["filaments"] = [
        {
            "id": "f",
            "material_id": "m",
            "name": "F",
            "density": 1.2,
            "diameter_tolerance": 0.1,
        }
    ]
    doc["variants"] = [
        {"id": "v", "filament_id": "f", "name": "V", "color_hex": ["#fff"]}
    ]
    doc["sizes"] = [{"id": "s", "variant_id": "v"}]
    doc["stores"] = [
        {
            "id": "shop",
            "name": "Shop",
            "storefront_url": "",
            "logo": "",
            "ships_from": "",
            "ships_to": "",
        }
    ]
    doc["purchase_links"] = [{"id": "buy", "size_id": "s", "store_id": "shop"}]
    result = adapt_catalog(json.dumps(doc), inputs())
    assert {record.kind for record in result.records} == set(KINDS)
    assert {record.kind: record.parent_id for record in result.records}[
        "purchase_link"
    ] == next(r.id for r in result.records if r.kind == "size")
    assert [
        (link.relationship_name, link.target_record_id) for link in result.links
    ] == [("store_id", next(r.id for r in result.records if r.kind == "store"))]


def test_nested_known_type_and_nonfinite_number_are_rejected():
    doc = minimal()
    doc["filaments"] = [
        {
            "id": "f",
            "name": "F",
            "density": 1.0,
            "diameter_tolerance": 0.1,
            "slicer_settings": {"generic": {"nozzle_temp": "220"}},
        }
    ]
    with pytest.raises(AdapterError, match="wrong"):
        adapt_catalog(json.dumps(doc), inputs())
    with pytest.raises(AdapterError, match="finite"):
        adapt_catalog(
            '{"version":"1","generated_at":"now","brands":[],"materials":[],"filaments":[],"variants":[],"sizes":[],"stores":[],"purchase_links":[],"x":1e400}',
            inputs(),
        )


def test_same_source_ids_are_scoped_to_parent_identity():
    doc = minimal()
    doc["brands"] = [
        {"id": "b1", "name": "B1", "logo": "", "website": "", "origin": ""},
        {"id": "b2", "name": "B2", "logo": "", "website": "", "origin": ""},
    ]
    doc["materials"] = [
        {"id": "m", "brand_id": "b1", "material": "PLA"},
        {"id": "m", "brand_id": "b2", "material": "PLA"},
    ]
    result = adapt_catalog(json.dumps(doc), inputs())
    materials = [record for record in result.records if record.kind == "material"]
    assert len(materials) == 2
    assert materials[0].id != materials[1].id
    assert materials[0].identity_key != materials[1].identity_key


def test_pinned_bulk_fixture_preserves_export_extensions():
    doc = json.loads((FIXTURES / "bulk.json").read_text())
    result = adapt_catalog(doc, inputs())
    filament = next(record for record in result.records if record.kind == "filament")
    assert filament.source_id == "filament-a"
    assert filament.extension_json["vendor_flag"] == {"active": False}
    assert len(result.records) == 7
