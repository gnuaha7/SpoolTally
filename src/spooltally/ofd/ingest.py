from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from spooltally.ofd.logos import LogoResult, LogoSource
from spooltally.ofd.refresh_merge import _persist_catalog
from spooltally.ofd.validation import validate_catalog


def ingest_catalog(
    session: Session,
    payload: bytes | str | dict[str, Any],
    schema_documents: Mapping[str, bytes | str],
    *,
    fetched_at: datetime,
    logos: Iterable[tuple[LogoSource, LogoResult]] = (),
) -> None:
    adapted, fetched = validate_catalog(
        payload, dict(schema_documents), fetched_at=fetched_at
    )
    _persist_catalog(session, adapted, fetched, logos)
