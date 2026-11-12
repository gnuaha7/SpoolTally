import copy
import json
import math
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from .schemas import SourceSchema, preserve_schemas
from .source_schema import decode_schema_documents

_NAMESPACE = UUID("d2029ff0-2c7d-58a3-9a46-8fdba06d6ca5")


class AdapterError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogRecordValue:
    id: str
    kind: str
    source_id: str
    identity_key: str
    upstream_uuid: str | None
    parent_id: str | None
    source_baseline_json: dict[str, Any]
    typed_fields_json: dict[str, Any]
    extension_json: dict[str, Any]
    raw_json: dict[str, Any]
    source_updated_at: str | None = None


@dataclass(frozen=True)
class CatalogLinkValue:
    source_record_id: str
    relationship_name: str
    ordinal: int
    target_record_id: str


@dataclass(frozen=True)
class AdaptedCatalog:
    version: str
    generated_at: str
    raw: dict[str, Any]
    records: tuple[CatalogRecordValue, ...]
    links: tuple[CatalogLinkValue, ...]
    schemas: tuple[SourceSchema, ...]


@dataclass(frozen=True)
class _Kind:
    collection: str
    parent_kind: str | None
    parent_field: str | None
    schema: str


KINDS = {
    "brand": _Kind("brands", None, None, "brand_schema"),
    "material": _Kind("materials", "brand", "brand_id", "material_schema"),
    "filament": _Kind("filaments", "material", "material_id", "filament_schema"),
    "variant": _Kind("variants", "filament", "filament_id", "variant_schema"),
    "size": _Kind("sizes", "variant", "variant_id", "sizes_schema"),
    "store": _Kind("stores", None, None, "store_schema"),
    "purchase_link": _Kind("purchase_links", "size", "size_id", "sizes_schema"),
}


def _load(payload):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError(f"duplicate JSON key {k!r}")
            out[k] = v
        return out

    try:
        doc = (
            payload
            if isinstance(payload, dict)
            else json.loads(
                payload,
                object_pairs_hook=pairs,
                parse_constant=lambda x: (_ for _ in ()).throw(
                    ValueError(f"invalid number {x}")
                ),
            )
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"invalid catalog JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise AdapterError("catalog must be an object")

    def check(v, p):
        if v is None or isinstance(v, (str, bool, int)):
            return
        if isinstance(v, float):
            if not math.isfinite(v):
                raise AdapterError(f"{p} must be finite")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                check(x, f"{p}[{i}]")
        elif isinstance(v, dict):
            for k, x in v.items():
                check(x, f"{p}.{k}")
        else:
            raise AdapterError(f"{p} is not JSON data")

    check(doc, "catalog")
    return doc


def _matches_type(value, schema):
    types = schema.get("type", [])
    types = types if isinstance(types, list) else [types]
    checks = {
        "object": lambda x: isinstance(x, dict),
        "array": lambda x: isinstance(x, list),
        "string": lambda x: isinstance(x, str),
        "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
        "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
        "boolean": lambda x: isinstance(x, bool),
    }
    return not types or any(checks[t](value) for t in types if t in checks)


def _resolve_schema(spec, root_schema, schemas):
    if spec.startswith("#/"):
        target = root_schema
        for part in spec[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return target
    name = spec.rsplit("/", 1)[-1].removesuffix(".json").removesuffix(".schema")
    return schemas.get(name, {})


def _check_value(value, spec, path, schemas, root_schema):
    if not isinstance(spec, dict):
        return
    ref = spec.get("$ref")
    if isinstance(ref, str):
        target = _resolve_schema(ref, root_schema, schemas)
        if not _matches_type(value, target):
            raise AdapterError(f"{path} has wrong referenced type")
        ref_root = target if ref.startswith("./") else root_schema
        _check_value(value, target, path, schemas, ref_root)
    types = spec.get("type", [])
    types = types if isinstance(types, list) else [types]
    if value is None:
        if "null" not in types and types:
            raise AdapterError(f"{path} cannot be null")
        return
    unions = spec.get("oneOf", spec.get("anyOf", []))
    if types and not _matches_type(value, spec):
        if not unions or not any(_matches_type(value, option) for option in unions):
            raise AdapterError(f"{path} has wrong type")
    if isinstance(value, dict):
        props = spec.get("properties", {})
        for key, child in value.items():
            child_spec = props.get(key) if isinstance(props, dict) else None
            if isinstance(child_spec, dict):
                _check_value(child, child_spec, f"{path}.{key}", schemas, root_schema)
            elif isinstance(spec.get("additionalProperties"), dict):
                _check_value(
                    child,
                    spec["additionalProperties"],
                    f"{path}.{key}",
                    schemas,
                    root_schema,
                )
    if isinstance(value, list) and isinstance(spec.get("items"), dict):
        for i, item in enumerate(value):
            _check_value(item, spec["items"], f"{path}[{i}]", schemas, root_schema)


def _check(value, schema, path, schemas):
    _check_value(value, schema, path, schemas, schema)


def adapt_catalog(
    payload: bytes | str | dict[str, Any], schema_documents: dict[str, bytes | str]
) -> AdaptedCatalog:
    try:
        doc = _load(payload)
        schemas = decode_schema_documents(schema_documents)
        version, generated = doc.get("version"), doc.get("generated_at")
        if not isinstance(version, str) or not isinstance(generated, str):
            raise AdapterError("version and generated_at must be strings")
        for spec in KINDS.values():
            if not isinstance(doc.get(spec.collection), list):
                raise AdapterError(f"{spec.collection} must be an array")
        records = []
        rows: list[tuple[CatalogRecordValue, dict[str, Any], str | None, _Kind]] = []
        index: dict[tuple[str, str], list[CatalogRecordValue]] = {}
        identities = set()
        uuids = set()
        for kind, spec in KINDS.items():
            source_schema = schemas.get(spec.schema, {})
            schema = source_schema.get("items", source_schema)
            if kind == "purchase_link":
                size_properties = schema.get("properties", {})
                link_schema = size_properties.get("purchase_links", {}).get("items", {})
                schema = {
                    **link_schema,
                    "definitions": source_schema.get("definitions", {}),
                    "properties": {
                        **link_schema.get("properties", {}),
                        "id": {"type": "string"},
                        "size_id": {"type": "string"},
                    },
                }
            for i, raw in enumerate(doc[spec.collection]):
                path = f"{spec.collection}[{i}]"
                if not isinstance(raw, dict):
                    raise AdapterError(f"{path} must be an object")
                sid = raw.get("id")
                if not isinstance(sid, str) or not sid:
                    raise AdapterError(f"{path}.id must be a non-empty string")
                uid = raw.get("uuid")
                if uid is not None:
                    try:
                        uid = str(UUID(uid))
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise AdapterError(f"{path}.uuid is invalid") from exc
                    if uid in uuids:
                        raise AdapterError(f"duplicate uuid {uid}")
                    uuids.add(uid)
                _check(raw, schema, path, schemas)
                pref = raw.get(spec.parent_field) if spec.parent_field else None
                if spec.parent_field and (not isinstance(pref, str) or not pref):
                    raise AdapterError(f"{path}.{spec.parent_field} is required")
                parent_identity = ""
                if spec.parent_kind:
                    parents = index.get((spec.parent_kind, str(pref)), [])
                    brand_ref = raw.get("brand_id")
                    if (
                        spec.parent_kind == "material"
                        and brand_ref
                        and len(parents) > 1
                    ):
                        parents = [
                            candidate
                            for candidate in parents
                            if any(
                                row[0].id == candidate.id and row[2] == brand_ref
                                for row in rows
                            )
                        ]
                    if len(parents) != 1:
                        raise AdapterError(
                            f"{path}.{spec.parent_field} is dangling or ambiguous"
                        )
                    parent_identity = parents[0].identity_key
                ident = (
                    f"uuid:{uid}"
                    if uid
                    else (
                        f"path:{kind.encode().hex()}:{parent_identity.encode().hex()}:{sid.encode().hex()}"
                    )
                )
                if ident in identities:
                    raise AdapterError(f"duplicate identity {ident}")
                identities.add(ident)
                rid = uid or str(uuid5(_NAMESPACE, "v1:" + ident))
                known = set(schema.get("properties", {})) | {"id", "uuid"}
                if kind in {"brand", "store"}:
                    known.add("logo_name")
                rec = CatalogRecordValue(
                    rid,
                    kind,
                    sid,
                    ident,
                    uid,
                    None,
                    copy.deepcopy(raw),
                    {k: copy.deepcopy(v) for k, v in raw.items() if k in known},
                    {k: copy.deepcopy(v) for k, v in raw.items() if k not in known},
                    copy.deepcopy(raw),
                    raw.get("source_updated_at"),
                )
                records.append(rec)
                rows.append((rec, raw, pref, spec))
                index.setdefault((kind, sid), []).append(rec)

        def resolve(kind, ref, field, context=None):
            found = index.get((kind, str(ref)), [])
            if context and kind == "material" and context.get("brand_id"):
                found = [
                    candidate
                    for candidate in found
                    if any(
                        row[0].id == candidate.id and row[2] == context["brand_id"]
                        for row in rows
                    )
                ]
            if not isinstance(ref, str) or len(found) != 1:
                raise AdapterError(f"{field} is dangling or ambiguous")
            return found[0]

        final = []
        links = []
        for rec, raw, pref, spec in rows:
            parent = (
                resolve(spec.parent_kind, pref, spec.parent_field, raw)
                if spec.parent_kind
                else None
            )
            final.append(
                CatalogRecordValue(
                    rec.id,
                    rec.kind,
                    rec.source_id,
                    rec.identity_key,
                    rec.upstream_uuid,
                    parent.id if parent else None,
                    rec.source_baseline_json,
                    rec.typed_fields_json,
                    rec.extension_json,
                    rec.raw_json,
                    rec.source_updated_at,
                )
            )
            for field, target_kind in [
                ("brand_id", "brand"),
                ("parent_brand_id", "brand"),
                ("material_id", "material"),
                ("filament_id", "filament"),
                ("variant_id", "variant"),
                ("size_id", "size"),
                ("store_id", "store"),
            ]:
                if field in raw and raw[field] is not None:
                    target = resolve(target_kind, raw[field], field, raw)
                    links.append(CatalogLinkValue(rec.id, field, 0, target.id))
        return AdaptedCatalog(
            version,
            generated,
            copy.deepcopy(doc),
            tuple(final),
            tuple(links),
            preserve_schemas(schemas, version),
        )
    except AdapterError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise AdapterError(str(exc)) from exc
