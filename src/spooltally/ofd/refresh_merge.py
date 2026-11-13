from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from spooltally.db.models import CatalogRecord, CatalogRecordLink, CatalogSchema
from spooltally.ofd.adapter import AdaptedCatalog
from spooltally.ofd.logos import LogoResult, LogoSource, upsert_source_asset


def _persist_catalog(
    session: Session,
    adapted: AdaptedCatalog,
    fetched_at: datetime,
    logos: Iterable[tuple[LogoSource, LogoResult]],
) -> None:
    if not session.in_transaction():
        raise RuntimeError("ingest_catalog requires a caller-owned transaction")

    existing = {
        (record.kind, record.identity_key): record
        for record in session.scalars(select(CatalogRecord)).all()
    }
    persisted: dict[str, CatalogRecord] = {}
    seen_ids: set[str] = set()
    for value in adapted.records:
        source_updated_at = (
            datetime.fromisoformat(value.source_updated_at.replace("Z", "+00:00"))
            if value.source_updated_at is not None
            else None
        )
        row = existing.get((value.kind, value.identity_key))
        if row is None:
            row = CatalogRecord(
                id=value.id,
                kind=value.kind,
                source_id=value.source_id,
                identity_key=value.identity_key,
                upstream_uuid=value.upstream_uuid,
                parent_id=value.parent_id,
                source_baseline_json=value.source_baseline_json,
                typed_fields_json=value.typed_fields_json,
                extension_json=value.extension_json,
                raw_json=value.raw_json,
                source_updated_at=source_updated_at,
                first_seen_at=fetched_at,
                last_seen_at=fetched_at,
            )
            session.add(row)
        else:
            row.source_id = value.source_id
            row.upstream_uuid = value.upstream_uuid
            row.parent_id = value.parent_id
            row.source_baseline_json = value.source_baseline_json
            row.typed_fields_json = value.typed_fields_json
            row.extension_json = value.extension_json
            row.raw_json = value.raw_json
            row.source_updated_at = source_updated_at
            row.last_seen_at = fetched_at
        persisted[value.id] = row
        seen_ids.add(row.id)

    session.flush()
    for source_id in seen_ids:
        session.execute(
            delete(CatalogRecordLink).where(
                CatalogRecordLink.source_record_id == source_id
            )
        )
    for link in adapted.links:
        session.add(
            CatalogRecordLink(
                id=str(uuid4()),
                source_record_id=persisted[link.source_record_id].id,
                relationship_name=link.relationship_name,
                ordinal=link.ordinal,
                target_record_id=persisted[link.target_record_id].id,
            )
        )

    for schema in adapted.schemas:
        current = session.get(CatalogSchema, schema.schema_name)
        if current is None:
            current = CatalogSchema(
                schema_name=schema.schema_name,
                source_json=schema.source_json,
                source_version=schema.source_version,
                fetched_at=fetched_at,
            )
            session.add(current)
        else:
            current.source_json = schema.source_json
            current.source_version = schema.source_version
            current.fetched_at = fetched_at
    session.flush()
    for source, result in logos:
        if result.status != "downloaded":
            continue
        value = next(
            value
            for value in adapted.records
            if value.kind == source.kind and value.source_id == source.source_id
        )
        row = persisted[value.id]
        upsert_source_asset(session, row, source, result)
