from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SOURCE_SCHEMA_NAMES = frozenset(
    {
        "brand_schema",
        "filament_schema",
        "material_schema",
        "material_aio_schema",
        "material_defaults_schema",
        "material_types_schema",
        "mechanical_schema",
        "sustainability_schema",
        "trait_rules",
        "variant_schema",
        "sizes_schema",
        "store_schema",
    }
)


@dataclass(frozen=True)
class SourceSchema:
    schema_name: str
    source_version: str
    source_json: Any


def preserve_schemas(
    documents: Mapping[str, Any], source_version: str
) -> tuple[SourceSchema, ...]:
    missing = SOURCE_SCHEMA_NAMES - set(documents)
    if missing:
        raise ValueError(f"missing schemas: {', '.join(sorted(missing))}")
    return tuple(
        SourceSchema(k, source_version, documents[k]) for k in sorted(documents)
    )
