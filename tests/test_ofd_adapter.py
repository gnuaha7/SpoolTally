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


def test_adapter_round_trip_preserves_all_source_fields():
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


def test_adapter_preserves_uuid_when_source_name_changes():
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


def test_adapter_preserves_field_paths_explicit_null_list_and_nested_values():
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
            "certifications": ["A"],
            "slicer_settings": {"orcaslicer": {"overrides": {"nozzle_temp": 210}}},
        }
    ]
    doc["variants"] = [
        {
            "id": "v",
            "filament_id": "f",
            "name": "n",
            "color_hex": ["#fff"],
            "hex_variants": ["#fefefe"],
            "discontinued": False,
            "transmission_distance": 0,
        }
    ]
    result = adapt_catalog(json.dumps(doc), inputs())
    filament = next(r for r in result.records if r.kind == "filament")
    variant = next(r for r in result.records if r.kind == "variant")
    assert filament.source_baseline_json["certifications"] == ["A"]
    assert (
        filament.source_baseline_json["slicer_settings"]["orcaslicer"]["overrides"][
            "nozzle_temp"
        ]
        == 210
    )
    assert variant.source_baseline_json["discontinued"] is False
    assert variant.source_baseline_json["transmission_distance"] == 0
    assert variant.source_baseline_json["hex_variants"] == ["#fefefe"]
    assert "uuid" not in variant.source_baseline_json


def test_adapter_rejects_corrupt_source_payload():
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
    doc["sizes"] = [
        {"id": "s", "variant_id": "v", "container_details": {"recycled_pct": None}}
    ]
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
    ] == [
        ("brand_id", next(r.id for r in result.records if r.kind == "brand")),
        ("material_id", next(r.id for r in result.records if r.kind == "material")),
        ("filament_id", next(r.id for r in result.records if r.kind == "filament")),
        ("variant_id", next(r.id for r in result.records if r.kind == "variant")),
        ("size_id", next(r.id for r in result.records if r.kind == "size")),
        ("store_id", next(r.id for r in result.records if r.kind == "store")),
    ]
    size = next(r for r in result.records if r.kind == "size")
    assert size.source_baseline_json["container_details"]["recycled_pct"] is None


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


def test_fallback_identity_uses_pinned_literal_uuid():
    doc = minimal()
    doc["brands"] = [{"id": "b", "name": "B"}]
    brand = adapt_catalog(doc, inputs()).records[0]
    assert brand.identity_key == "path:6272616e64::62"
    assert brand.id == "d7655c24-9db2-597e-a4f7-d0fc13103b3c"


def test_pinned_bulk_fixture_preserves_export_extensions():
    doc = json.loads((FIXTURES / "bulk.json").read_text())
    result = adapt_catalog(doc, inputs())
    filament = next(record for record in result.records if record.kind == "filament")
    assert filament.source_id == "filament-a"
    assert filament.extension_json["vendor_flag"] == {"active": False}
    assert len(result.records) == 7


def test_review_findings_are_rejected_or_preserved():
    doc = minimal()
    doc["brands"] = [{"id": "b", "name": "B", "logo_name": "b.svg"}]
    doc["materials"] = [{"id": "m", "brand_id": "b", "material": "PLA"}]
    doc["filaments"] = [
        {
            "id": "f",
            "material_id": "m",
            "name": "F",
            "density": 1,
            "diameter_tolerance": 0.1,
        }
    ]
    doc["variants"] = [{"id": "v", "filament_id": "f", "name": "V", "color_hex": [42]}]
    with pytest.raises(AdapterError):
        adapt_catalog(doc, inputs())
    doc["variants"][0]["color_hex"] = ["#fff"]
    result = adapt_catalog(doc, inputs())
    assert result.records[0].typed_fields_json["logo_name"] == "b.svg"
    assert {link.relationship_name for link in result.links} == {
        "brand_id",
        "material_id",
        "filament_id",
    }
    assert any(
        s.schema_name == "future_schema"
        for s in adapt_catalog(
            minimal(), {**inputs(), "future_schema": b'{"type":"object"}'}
        ).schemas
    )
    bad = {**inputs(), "mechanical_schema": b'{"type":"object","x":1e400}'}
    with pytest.raises(ValueError, match="finite"):
        adapt_catalog(minimal(), bad)


@pytest.mark.parametrize("kind", ["brand", "store"])
@pytest.mark.parametrize(
    "logo_name", [42, ["logo.svg"], {"name": "logo.svg"}, None, True]
)
def test_adapter_rejects_invalid_exported_logo_name_types(kind, logo_name):
    doc = minimal()
    collection = KINDS[kind].collection
    doc[collection] = [{"id": "logo-owner", "logo_name": logo_name}]

    with pytest.raises(AdapterError, match="logo_name"):
        adapt_catalog(doc, inputs())


@pytest.mark.parametrize("kind", ["brand", "store"])
def test_adapter_preserves_exported_logo_name_string_without_schema_mutation(kind):
    doc = minimal()
    collection = KINDS[kind].collection
    doc[collection] = [{"id": "logo-owner", "logo_name": "logo.svg"}]
    schemas = inputs()
    originals = dict(schemas)

    result = adapt_catalog(doc, schemas)

    record = next(record for record in result.records if record.kind == kind)
    assert record.typed_fields_json["logo_name"] == "logo.svg"
    assert record.raw_json["logo_name"] == "logo.svg"
    assert schemas == originals
    source_schema = next(
        schema.source_json
        for schema in result.schemas
        if schema.schema_name == KINDS[kind].schema
    )
    assert "logo_name" not in source_schema["properties"]


def test_parent_context_resolves_duplicate_material_ids_and_nested_shared_types():
    doc = minimal()
    doc["brands"] = [{"id": x, "name": x} for x in ("a", "b")]
    doc["materials"] = [
        {"id": "pla", "brand_id": x, "material": "PLA"} for x in ("a", "b")
    ]
    doc["filaments"] = [
        {
            "id": "f",
            "material_id": "pla",
            "brand_id": "a",
            "name": "F",
            "density": 1,
            "diameter_tolerance": 0.1,
        }
    ]
    assert adapt_catalog(doc, inputs()).records[-1].parent_id is not None
    doc["filaments"][0]["sustainability"] = {"recycled_content": {"pct": "wrong"}}
    with pytest.raises(AdapterError):
        adapt_catalog(doc, inputs())
    doc["filaments"][0]["sustainability"] = {"recycled_content": {"pct": None}}
    adapt_catalog(doc, inputs())
