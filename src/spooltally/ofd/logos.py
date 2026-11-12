from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from spooltally.db.models import Asset, CatalogRecord

_HOST = "api.openfilamentdatabase.org"
_MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class LogoSource:
    kind: Literal["brand", "store"]
    source_id: str
    index_entry: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LogoResult:
    status: Literal["downloaded", "not_modified", "failed"]
    url: str
    source_metadata: dict[str, Any]
    blob: bytes | None = None
    media_type: str | None = None
    sha256: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None


def _url(source: LogoSource) -> str:
    owner = {"brand": "brand_id", "store": "store_id"}.get(source.kind)
    if owner is None or not isinstance(source.source_id, str) or not source.source_id:
        raise ValueError("invalid logo owner")
    index, metadata = source.index_entry, source.metadata
    logo_id = index.get("id")
    extension = metadata.get("extension")
    index_name = index.get("brand_name", index.get("store_name", index.get("name")))
    metadata_name = metadata.get(
        "brand_name", metadata.get("store_name", metadata.get("name"))
    )
    if (
        not isinstance(logo_id, str)
        or not logo_id
        or not isinstance(index_name, str)
        or index_name != metadata_name
        or index.get("slug") != logo_id
        or metadata.get("id") != logo_id
        or metadata.get("slug") != logo_id
        or index.get(owner) != source.source_id
        or metadata.get(owner) != source.source_id
        or index.get("path") != f"{logo_id}.json"
        or extension not in {"png", "jpg", "svg"}
        or not isinstance(metadata.get("filename"), str)
        or metadata.get("filename", "").rsplit(".", 1)[-1] != extension
        or metadata.get("logo_file") != f"{logo_id}.{extension}"
    ):
        raise ValueError("logo index and metadata do not match")
    collection = "brands" if source.kind == "brand" else "stores"
    segment = quote(logo_id, safe="")
    return f"https://{_HOST}/api/v1/{collection}/logo/{segment}.{extension}"


def _valid_asset(asset: Asset | None, source_record_id: str) -> bool:
    if (
        asset is None
        or asset.source_record_id != source_record_id
        or asset.blob is None
    ):
        return False
    expected = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}.get(
        asset.media_type
    )
    return (
        expected is not None
        and asset.blob.startswith(expected)
        and len(asset.blob) <= _MAX_BYTES
        and hashlib.sha256(asset.blob).digest() == asset.sha256
    )


def _matches_source(asset: Asset | None, source: LogoSource) -> bool:
    if asset is None or not isinstance(asset.source_metadata_json, dict):
        return False
    return (
        asset.source_metadata_json.get("index") == source.index_entry
        and asset.source_metadata_json.get("metadata") == source.metadata
    )


def fetch_logo(
    source: LogoSource,
    client: httpx.Client,
    prior_asset: Asset | None = None,
    source_record_id: str | None = None,
) -> LogoResult:
    try:
        url = _url(source)
    except (ValueError, TypeError):
        return LogoResult(
            "failed", "", {"index": source.index_entry, "metadata": source.metadata}
        )
    metadata = {"index": source.index_entry, "metadata": source.metadata}
    if url.endswith(".svg"):
        return LogoResult("failed", url, metadata)
    headers: dict[str, str] = {}
    prior_ok = (
        source_record_id is not None
        and _valid_asset(prior_asset, source_record_id)
        and _matches_source(prior_asset, source)
    )
    previous_http: Any = {}
    if prior_ok and prior_asset is not None:
        if isinstance(prior_asset.source_metadata_json, dict):
            previous_http = prior_asset.source_metadata_json.get("http", {})
    if isinstance(previous_http, dict):
        if isinstance(previous_http.get("etag"), str):
            headers["If-None-Match"] = previous_http["etag"]
        if isinstance(previous_http.get("last_modified"), str):
            headers["If-Modified-Since"] = previous_http["last_modified"]
    try:
        request = client.build_request("GET", url, headers=headers)
        if request.url.query or request.url.fragment:
            return LogoResult("failed", url, metadata)
        response = client.send(request, stream=True, follow_redirects=False)
        try:
            if response.status_code == 304:
                if prior_ok and headers:
                    return LogoResult("not_modified", url, metadata)
                return LogoResult("failed", url, metadata)
            if response.status_code != 200:
                return LogoResult("failed", url, metadata)
            ext = source.metadata["extension"]
            media = "image/png" if ext == "png" else "image/jpeg"
            signature = b"\x89PNG\r\n\x1a\n" if ext == "png" else b"\xff\xd8\xff"
            if (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != media
            ):
                return LogoResult("failed", url, metadata)
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                body.extend(chunk)
                if len(body) > _MAX_BYTES:
                    return LogoResult("failed", url, metadata)
            blob = bytes(body)
            if not blob.startswith(signature):
                return LogoResult("failed", url, metadata)
            return LogoResult(
                "downloaded",
                url,
                metadata,
                blob,
                media,
                hashlib.sha256(blob).digest(),
                response.headers.get("etag"),
                response.headers.get("last-modified"),
            )
        finally:
            response.close()
    except (httpx.HTTPError, ValueError):
        return LogoResult("failed", url, metadata)


def upsert_source_asset(
    session: Session,
    source_record: CatalogRecord,
    source: LogoSource,
    result: LogoResult,
) -> Asset | None:
    owner_field = {"brand": "brand_id", "store": "store_id"}.get(source.kind)
    if (
        owner_field is None
        or source_record.kind != source.kind
        or source_record.source_id != source.source_id
        or source.index_entry.get(owner_field) != source.source_id
        or source.metadata.get(owner_field) != source.source_id
    ):
        raise ValueError("logo source does not match catalog record")
    if (
        result.status != "downloaded"
        or result.blob is None
        or result.sha256 is None
        or result.media_type is None
    ):
        return None
    if result.source_metadata != {
        "index": source.index_entry,
        "metadata": source.metadata,
    } or result.url != _url(source):
        raise ValueError("logo result does not match source")
    signature = {
        "image/png": b"\x89PNG\r\n\x1a\n",
        "image/jpeg": b"\xff\xd8\xff",
    }.get(result.media_type)
    extension = source.metadata.get("extension")
    expected_media = (
        "image/png"
        if extension == "png"
        else "image/jpeg"
        if extension == "jpg"
        else None
    )
    if (
        signature is None
        or result.media_type != expected_media
        or not result.blob.startswith(signature)
        or len(result.blob) > _MAX_BYTES
        or hashlib.sha256(result.blob).digest() != result.sha256
    ):
        return None
    current = session.scalar(
        select(Asset).where(Asset.source_record_id == source_record.id)
    )
    values = dict(
        sha256=result.sha256,
        media_type=result.media_type,
        source_metadata_json={
            **result.source_metadata,
            "url": result.url,
            "http": {"etag": result.etag, "last_modified": result.last_modified},
        },
        fetched_at=datetime.now(UTC),
        blob=result.blob,
    )
    if current is None:
        current = Asset(
            id=str(uuid4()),
            source_record_id=source_record.id,
            local_manufacturer_id=None,
            local_filament_id=None,
            **values,
        )
        session.add(current)
    else:
        for key, value in values.items():
            setattr(current, key, value)
    return current
