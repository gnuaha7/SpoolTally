from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class CanonicalJSON(TypeDecorator[Any]):
    impl = Text
    cache_ok = True

    def __init__(self, *, none_as_null: bool = True) -> None:
        super().__init__()
        self.none_as_null = none_as_null

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None and self.none_as_null:
            return None
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def process_result_value(self, value: str | None, dialect: Any) -> Any:
        return None if value is None else json.loads(value)


class UTCDateTime(TypeDecorator[datetime]):
    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
            .replace(".000000Z", "Z")
        )

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        return (
            None
            if value is None
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )


def object_json(*, none_as_null: bool = True) -> CanonicalJSON:
    return CanonicalJSON(none_as_null=none_as_null)


class CatalogRecord(Base):
    __tablename__ = "catalog_records"
    __table_args__ = (
        UniqueConstraint("kind", "identity_key"),
        UniqueConstraint("upstream_uuid"),
        CheckConstraint(
            "kind IN ('brand','material','filament','variant','size','store','purchase_link')"  # noqa: E501
        ),
        CheckConstraint(
            "json_valid(source_baseline_json) AND json_type(source_baseline_json) = 'object'"  # noqa: E501
        ),
        CheckConstraint(
            "json_valid(typed_fields_json) AND json_type(typed_fields_json) = 'object'"
        ),
        CheckConstraint(
            "json_valid(extension_json) AND json_type(extension_json) = 'object'"
        ),
        CheckConstraint("json_valid(raw_json) AND json_type(raw_json) = 'object'"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    upstream_uuid: Mapped[str | None] = mapped_column(String(36))
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_records.id", ondelete="RESTRICT")
    )
    source_baseline_json: Mapped[dict[str, Any]] = mapped_column(
        object_json(), nullable=False
    )
    typed_fields_json: Mapped[dict[str, Any]] = mapped_column(
        object_json(), nullable=False
    )
    extension_json: Mapped[dict[str, Any]] = mapped_column(
        object_json(), nullable=False, server_default=text("'{}'")
    )
    raw_json: Mapped[dict[str, Any]] = mapped_column(object_json(), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    parent_record: Mapped[CatalogRecord | None] = relationship(
        remote_side="CatalogRecord.id", back_populates="child_records"
    )
    child_records: Mapped[list[CatalogRecord]] = relationship(
        back_populates="parent_record"
    )
    source_links: Mapped[list[CatalogRecordLink]] = relationship(
        foreign_keys="CatalogRecordLink.source_record_id",
        back_populates="source_record",
    )
    target_links: Mapped[list[CatalogRecordLink]] = relationship(
        foreign_keys="CatalogRecordLink.target_record_id",
        back_populates="target_record",
    )


class CatalogRecordLink(Base):
    __tablename__ = "catalog_record_links"
    __table_args__ = (
        UniqueConstraint("source_record_id", "relationship_name", "ordinal"),
        CheckConstraint("ordinal >= 0"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_record_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_records.id", ondelete="RESTRICT"), nullable=False
    )
    relationship_name: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    target_record_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_records.id", ondelete="RESTRICT"), nullable=False
    )
    source_record: Mapped[CatalogRecord] = relationship(
        foreign_keys=[source_record_id], back_populates="source_links"
    )
    target_record: Mapped[CatalogRecord] = relationship(
        foreign_keys=[target_record_id], back_populates="target_links"
    )


class CatalogSchema(Base):
    __tablename__ = "catalog_schemas"
    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    source_json: Mapped[Any] = mapped_column(object_json(), nullable=False)
    source_version: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class LocalManufacturer(Base):
    __tablename__ = "local_manufacturers"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 100"),
        CheckConstraint("trim(name) = name"),
        CheckConstraint("revision >= 1"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_brand_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_records.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source_brand: Mapped[CatalogRecord | None] = relationship(
        foreign_keys=[source_brand_id]
    )
    logo_asset: Mapped[Asset | None] = relationship(
        foreign_keys="Asset.local_manufacturer_id",
        back_populates="local_manufacturer",
        uselist=False,
    )
    filaments: Mapped[list[LocalFilament]] = relationship(back_populates="manufacturer")


class LocalFilament(Base):
    __tablename__ = "local_filaments"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 120"),
        CheckConstraint("trim(name) = name"),
        CheckConstraint("revision >= 1"),
        CheckConstraint(
            "default_tare_mg IS NULL OR (typeof(default_tare_mg) = 'integer' "
            "AND default_tare_mg >= 0)"
        ),
        CheckConstraint(
            "nominal_weight_mg IS NULL OR (typeof(nominal_weight_mg) = 'integer' "
            "AND nominal_weight_mg >= 0)"
        ),
        CheckConstraint(
            "json_valid(presentation_json) AND json_type(presentation_json) = 'object'"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    manufacturer_id: Mapped[str] = mapped_column(
        ForeignKey("local_manufacturers.id", ondelete="RESTRICT"), nullable=False
    )
    source_filament_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_records.id", ondelete="RESTRICT")
    )
    selected_variant_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_records.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    presentation_json: Mapped[dict[str, Any]] = mapped_column(
        object_json(), nullable=False, server_default=text("'{}'")
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    default_tare_mg: Mapped[int | None] = mapped_column(Integer)
    nominal_weight_mg: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    manufacturer: Mapped[LocalManufacturer] = relationship(back_populates="filaments")
    source_filament: Mapped[CatalogRecord | None] = relationship(
        foreign_keys=[source_filament_id]
    )
    selected_variant: Mapped[CatalogRecord | None] = relationship(
        foreign_keys=[selected_variant_id]
    )
    overrides: Mapped[list[SourceFieldOverride]] = relationship(
        back_populates="local_filament"
    )
    spools: Mapped[list[Spool]] = relationship(back_populates="filament")
    logo_asset: Mapped[Asset | None] = relationship(
        foreign_keys="Asset.local_filament_id",
        back_populates="local_filament",
        uselist=False,
    )


class SourceFieldOverride(Base):
    __tablename__ = "source_field_overrides"
    __table_args__ = (
        UniqueConstraint("local_filament_id", "json_pointer"),
        CheckConstraint("json_valid(value_json)"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    local_filament_id: Mapped[str] = mapped_column(
        ForeignKey("local_filaments.id", ondelete="RESTRICT"), nullable=False
    )
    json_pointer: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[Any] = mapped_column(
        object_json(none_as_null=False), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    local_filament: Mapped[LocalFilament] = relationship(back_populates="overrides")


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (CheckConstraint("revision >= 1"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    spools: Mapped[list[Spool]] = relationship(back_populates="location")


class Spool(Base):
    __tablename__ = "spools"
    __table_args__ = (
        CheckConstraint(
            "tare_mg IS NULL OR (typeof(tare_mg) = 'integer' AND tare_mg >= 0)"
        ),
        CheckConstraint("typeof(initial_mg) = 'integer' AND initial_mg > 0"),
        CheckConstraint("typeof(remaining_mg) = 'integer' AND remaining_mg >= 0"),
        CheckConstraint(
            "price_eur_cents IS NULL OR (typeof(price_eur_cents) = 'integer' "
            "AND price_eur_cents >= 0)"
        ),
        CheckConstraint("revision >= 1"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filament_id: Mapped[str] = mapped_column(
        ForeignKey("local_filaments.id", ondelete="RESTRICT"), nullable=False
    )
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT")
    )
    tare_mg: Mapped[int | None] = mapped_column(Integer)
    initial_mg: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_mg: Mapped[int] = mapped_column(Integer, nullable=False)
    price_eur_cents: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    filament: Mapped[LocalFilament] = relationship(back_populates="spools")
    location: Mapped[Location | None] = relationship(back_populates="spools")
    movements: Mapped[list[Movement]] = relationship(back_populates="spool")


class Movement(Base):
    __tablename__ = "movements"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('created','edited','archived','restored','consumed','weighed','measured','relocated')"  # noqa: E501
        ),
        CheckConstraint("amount_mg IS NULL OR amount_mg > 0"),
        CheckConstraint(
            "(kind = 'created' AND before_json IS NULL AND after_json IS NOT NULL) OR (kind <> 'created' AND before_json IS NOT NULL AND after_json IS NOT NULL)"  # noqa: E501
        ),
        CheckConstraint(
            "(kind = 'consumed' AND amount_mg IS NOT NULL AND reason IS NOT NULL AND length(trim(reason)) BETWEEN 1 AND 500) OR (kind <> 'consumed' AND amount_mg IS NULL)"  # noqa: E501
        ),
        CheckConstraint(
            "amount_mg IS NULL OR (typeof(amount_mg) = 'integer' AND amount_mg > 0)"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    spool_id: Mapped[str] = mapped_column(
        ForeignKey("spools.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    amount_mg: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[dict[str, Any]] = mapped_column(
        object_json(), nullable=False, server_default=text("'{}'")
    )
    before_json: Mapped[dict[str, Any] | None] = mapped_column(object_json())
    after_json: Mapped[dict[str, Any]] = mapped_column(object_json(), nullable=False)
    spool: Mapped[Spool] = relationship(back_populates="movements")


class TokenRecord(Base):
    __tablename__ = "token_records"
    __table_args__ = (CheckConstraint("length(token_hash) = 32"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(
        Text(collation="BINARY"), nullable=False, unique=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    scopes: Mapped[list[TokenScope]] = relationship(back_populates="token")
    idempotency_records: Mapped[list[IdempotencyRecord]] = relationship(
        back_populates="token"
    )


class TokenScope(Base):
    __tablename__ = "token_scopes"
    token_id: Mapped[str] = mapped_column(
        ForeignKey("token_records.id", ondelete="RESTRICT"), primary_key=True
    )
    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    token: Mapped[TokenRecord] = relationship(back_populates="scopes")


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("length(key) BETWEEN 1 AND 128 AND key NOT GLOB '*[^!-~]*'"),
        CheckConstraint("length(fingerprint) = 32"),
    )
    token_id: Mapped[str] = mapped_column(
        ForeignKey("token_records.id", ondelete="RESTRICT"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False)
    replay_headers_json: Mapped[dict[str, Any]] = mapped_column(
        object_json(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    token: Mapped[TokenRecord] = relationship(back_populates="idempotency_records")


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint("length(sha256) = 32"),
        CheckConstraint(
            "(source_record_id IS NOT NULL) + (local_manufacturer_id IS NOT NULL) + (local_filament_id IS NOT NULL) = 1"  # noqa: E501
        ),
        Index(
            "uq_assets_source_record",
            "source_record_id",
            unique=True,
            sqlite_where=text("source_record_id IS NOT NULL"),
        ),
        Index(
            "uq_assets_local_manufacturer",
            "local_manufacturer_id",
            unique=True,
            sqlite_where=text("local_manufacturer_id IS NOT NULL"),
        ),
        Index(
            "uq_assets_local_filament",
            "local_filament_id",
            unique=True,
            sqlite_where=text("local_filament_id IS NOT NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_records.id", ondelete="RESTRICT")
    )
    local_manufacturer_id: Mapped[str | None] = mapped_column(
        ForeignKey("local_manufacturers.id", ondelete="RESTRICT")
    )
    local_filament_id: Mapped[str | None] = mapped_column(
        ForeignKey("local_filaments.id", ondelete="RESTRICT")
    )
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_metadata_json: Mapped[Any] = mapped_column(object_json(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source_record: Mapped[CatalogRecord | None] = relationship()
    local_manufacturer: Mapped[LocalManufacturer | None] = relationship(
        foreign_keys=[local_manufacturer_id], back_populates="logo_asset"
    )
    local_filament: Mapped[LocalFilament | None] = relationship(
        foreign_keys=[local_filament_id], back_populates="logo_asset"
    )


class InventoryState(Base):
    __tablename__ = "inventory_state"
    __table_args__ = (CheckConstraint("id = 1"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_generation: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True
    )


class RestoreControl(Base):
    __tablename__ = "restore_control"
    __table_args__ = (
        CheckConstraint("id = 1"),
        CheckConstraint("mode IN ('normal','restore')"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'normal'")
    )


class InventorySettings(Base):
    __tablename__ = "inventory_settings"
    __table_args__ = (
        CheckConstraint("id = 1"),
        CheckConstraint("low_stock_numerator > 0"),
        CheckConstraint(
            "low_stock_denominator > 0 AND low_stock_denominator <= 1000000"
        ),
        CheckConstraint("low_stock_numerator <= low_stock_denominator"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    low_stock_numerator: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    low_stock_denominator: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10")
    )


class CatalogScheduleState(Base):
    __tablename__ = "catalog_schedule_state"
    __table_args__ = (CheckConstraint("id = 1"), CheckConstraint("wall_time = '03:00'"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_handled_slot_key: Mapped[str | None] = mapped_column(Text)
    timezone_name: Mapped[str] = mapped_column(Text, nullable=False)
    wall_time: Mapped[str] = mapped_column(
        String(5), nullable=False, server_default=text("'03:00'")
    )


class CatalogRun(Base):
    __tablename__ = "catalog_runs"
    __table_args__ = (
        CheckConstraint("trigger IN ('scheduled','manual')"),
        CheckConstraint("status IN ('running','succeeded','failed','interrupted')"),
        CheckConstraint("attempt_count >= 1"),
        CheckConstraint("source_digest IS NULL OR length(source_digest) = 32"),
        Index(
            "uq_catalog_runs_running",
            "status",
            unique=True,
            sqlite_where=text("status = 'running'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(9), nullable=False)
    schedule_key: Mapped[str | None] = mapped_column(Text, unique=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(11), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_version: Mapped[str | None] = mapped_column(Text)
    source_digest: Mapped[bytes | None] = mapped_column(LargeBinary)
    error_code: Mapped[str | None] = mapped_column(String(128))
