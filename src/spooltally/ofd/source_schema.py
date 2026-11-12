import json
from typing import Any


def decode_schema_documents(documents: dict[str, bytes | str]) -> dict[str, Any]:
    result = {}
    for name, data in documents.items():

        def pairs(items):
            out = {}
            for key, value in items:
                if key in out:
                    raise ValueError(f"duplicate key {key}")
                out[key] = value
            return out

        try:
            result[name] = json.loads(
                data,
                object_pairs_hook=pairs,
                parse_constant=lambda x: (_ for _ in ()).throw(
                    ValueError(f"invalid number {x}")
                ),
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid schema {name}: {exc}") from exc
        if not isinstance(result[name], dict):
            raise ValueError(f"schema {name} must be an object")
    return result
