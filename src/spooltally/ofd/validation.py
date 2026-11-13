from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from spooltally.ofd.adapter import AdaptedCatalog, adapt_catalog


def _timestamp(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def validate_catalog(
    payload: bytes | str | dict[str, Any],
    schema_documents: dict[str, bytes | str],
    *,
    fetched_at: datetime,
) -> tuple[AdaptedCatalog, datetime]:
    fetched = _timestamp(fetched_at, "fetched_at")
    adapted = adapt_catalog(payload, schema_documents)
    for record in adapted.records:
        if record.source_updated_at is not None:
            try:
                parsed = datetime.fromisoformat(
                    record.source_updated_at.replace("Z", "+00:00")
                )
            except (AttributeError, ValueError) as exc:
                raise ValueError(
                    "source_updated_at must be an RFC 3339 timestamp"
                ) from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("source_updated_at must include a timezone")
    return adapted, fetched
