from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spooltally.db.models import Asset, Base, CatalogRecord
from spooltally.ofd.logos import LogoSource, fetch_logo, upsert_source_asset

PNG = b"\x89PNG\r\n\x1a\n" + b"x"
JPEG = b"\xff\xd8\xffx"


def source(ext: str = "png", **extra: object) -> LogoSource:
    index = {
        "id": "brand/logo ?",
        "slug": "brand/logo ?",
        "brand_id": "raw/id",
        "brand_name": "Brand",
        "path": "brand/logo ?.json",
    }
    metadata = {
        "id": index["id"],
        "slug": index["slug"],
        "brand_id": "raw/id",
        "brand_name": "Brand",
        "extension": ext,
        "filename": f"logo.{ext}",
        "logo_file": f"{index['id']}.{ext}",
    }
    metadata.update(extra)
    return LogoSource("brand", "raw/id", index, metadata)


def test_trusted_png_and_jpg_logos_are_stored(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'logos.sqlite3'}")
    Base.metadata.create_all(engine)
    for ext, body, mime in (("png", PNG, "image/png"), ("jpg", JPEG, "image/jpeg")):
        transport = httpx.MockTransport(
            lambda request, body=body, mime=mime: httpx.Response(
                200, content=body, headers={"Content-Type": mime}
            )
        )
        with httpx.Client(transport=transport) as client:
            result = fetch_logo(source(ext), client)
        assert result.status == "downloaded"
        assert result.blob == body
        assert result.media_type == mime
        assert result.sha256 == hashlib.sha256(body).digest()
        assert result.source_metadata["index"]["brand_id"] == "raw/id"
        now = datetime.now(UTC)
        record = CatalogRecord(
            id=str(uuid4()), kind="brand", source_id="raw/id",
            identity_key=f"brand:raw/id:{ext}", source_baseline_json={},
            typed_fields_json={}, extension_json={}, raw_json={},
            first_seen_at=now, last_seen_at=now,
        )
        with Session(engine) as session:
            session.add(record)
            session.commit()
            inserted = upsert_source_asset(session, record, source(ext), result)
            assert inserted is not None
            asset_id = inserted.id
            session.commit()
            session.expire_all()
            saved = session.scalar(
                select(Asset).where(Asset.source_record_id == record.id)
            )
            assert saved is not None
            assert (saved.id, saved.blob, saved.media_type, saved.sha256) == (
                asset_id, body, mime, hashlib.sha256(body).digest()
            )
            assert saved.source_metadata_json == {
                **result.source_metadata,
                "url": result.url,
                "http": {"etag": None, "last_modified": None},
            }
            first_fetched_at = saved.fetched_at
            replacement = fetch_logo(
                source(ext),
                httpx.Client(transport=httpx.MockTransport(
                    lambda request, body=body, mime=mime: httpx.Response(
                        200,
                        content=body + b"y",
                        headers={"Content-Type": mime},
                    )
                )),
            )
            replaced = upsert_source_asset(session, record, source(ext), replacement)
            assert replaced is not None and replaced.id == asset_id
            session.commit()
            session.expire_all()
            saved = session.scalar(
                select(Asset).where(Asset.source_record_id == record.id)
            )
            assert saved is not None
            assert (saved.id, saved.blob, saved.media_type, saved.sha256) == (
                asset_id, body + b"y", mime, hashlib.sha256(body + b"y").digest()
            )
            assert saved.fetched_at >= first_fetched_at
    engine.dispose()


def test_client_params_cannot_add_query_to_trusted_logo_request() -> None:
    requested: list[str] = []
    transport = httpx.MockTransport(
        lambda request: (requested.append(str(request.url)) or httpx.Response(
            200, content=PNG, headers={"Content-Type": "image/png"}
        ))
    )
    with httpx.Client(transport=transport, params={"tracking": "1"}) as client:
        result = fetch_logo(source(), client)
    assert result.status == "failed"
    assert requested == []


def test_untrusted_hosts_redirects_svg_and_oversize_are_rejected() -> None:
    for status, ext, body, mime in (
        (302, "png", PNG, "image/png"),
        (200, "svg", b"<svg/>", "image/svg+xml"),
        (200, "png", b"\x89PNG\r\n\x1a\n" + b"x" * 1_999_993, "image/png"),
    ):
        seen: list[str] = []

        def response(
            request: httpx.Request,
            seen: list[str] = seen,
            status: int = status,
            body: bytes = body,
            mime: str = mime,
        ) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(
                status,
                content=body,
                headers={
                    "Content-Type": mime,
                    "Location": "https://evil.example/logo.png",
                },
            )

        transport = httpx.MockTransport(response)
        with httpx.Client(transport=transport, follow_redirects=True) as client:
            result = fetch_logo(source(ext), client)
        assert result.status == "failed"
        assert result.blob is None
        if status == 302:
            assert len(seen) == 1
    requested: list[str] = []
    transport = httpx.MockTransport(
        lambda request: (
            requested.append(str(request.url))
            or httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})
        )
    )
    with httpx.Client(transport=transport) as client:
        ignored = fetch_logo(source("png", url="https://evil.example/logo.png"), client)
    assert ignored.status == "downloaded"
    assert requested[0].startswith("https://api.openfilamentdatabase.org/")


def test_actual_stream_limit_and_content_validation() -> None:
    exact = b"\x89PNG\r\n\x1a\n" + b"x" * (2_000_000 - 8)
    for body, mime, expected in (
        (exact, "image/png", "downloaded"),
        (PNG, "image/jpeg", "failed"),
        (b"not a png", "image/png", "failed"),
    ):
        transport = httpx.MockTransport(
            lambda request, body=body, mime=mime: httpx.Response(
                200,
                content=body,
                headers={"Content-Type": mime, "Content-Length": "1"},
            )
        )
        with httpx.Client(transport=transport) as client:
            result = fetch_logo(source(), client)
        assert result.status == expected
        if expected == "downloaded":
            assert len(result.blob or b"") == 2_000_000


def test_failed_logo_download_keeps_previous_asset() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    blob = PNG
    record = CatalogRecord(
        id=str(uuid4()),
        kind="brand",
        source_id="raw/id",
        identity_key="brand:raw/id",
        source_baseline_json={},
        typed_fields_json={},
        extension_json={},
        raw_json={},
        first_seen_at=now,
        last_seen_at=now,
    )
    prior = Asset(
        id=str(uuid4()),
        source_record=record,
        sha256=hashlib.sha256(blob).digest(),
        media_type="image/png",
        source_metadata_json={"index": {"old": True}, "http": {"etag": "v1"}},
        fetched_at=now,
        blob=blob,
    )
    with Session(engine) as session:
        session.add_all([record, prior])
        session.commit()
        before = (
            prior.id,
            prior.sha256,
            prior.media_type,
            prior.source_metadata_json,
            prior.fetched_at,
            prior.blob,
        )
        failed = fetch_logo(
            source(),
            httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(500))
            ),
        )
        assert upsert_source_asset(session, record, source(), failed) is None
        record.raw_json = {"catalog_revision": "accepted"}
        session.commit()
        session.expire_all()
        after = session.scalar(select(Asset))
        assert (
            after.id,
            after.sha256,
            after.media_type,
            after.source_metadata_json,
            after.fetched_at,
            after.blob,
        ) == before
        assert session.get(CatalogRecord, record.id).raw_json == {
            "catalog_revision": "accepted"
        }
    engine.dispose()


def test_conditional_304_requires_matching_valid_prior_asset() -> None:
    now = datetime.now(UTC)
    record_id = str(uuid4())
    asset = Asset(
        id=str(uuid4()),
        source_record_id=record_id,
        sha256=hashlib.sha256(PNG).digest(),
        media_type="image/png",
        source_metadata_json={
            "index": source().index_entry,
            "metadata": source().metadata,
            "http": {"etag": '"v1"'},
        },
        fetched_at=now,
        blob=PNG,
    )
    captured: list[httpx.Request] = []

    def not_modified(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(304)

    with httpx.Client(transport=httpx.MockTransport(not_modified)) as client:
        matched = fetch_logo(source(), client, asset, record_id)
    assert matched.status == "not_modified"
    assert captured[0].headers["if-none-match"] == '"v1"'

    with httpx.Client(transport=httpx.MockTransport(not_modified)) as client:
        absent = fetch_logo(source(), client)
    assert absent.status == "failed"
    assert len(captured) == 2
    assert "if-none-match" not in captured[-1].headers

    asset.sha256 = b"x" * 32
    with httpx.Client(transport=httpx.MockTransport(not_modified)) as client:
        corrupt = fetch_logo(source(), client, asset, record_id)
    assert corrupt.status == "failed"
    assert len(captured) == 3


def test_successful_asset_write_obeys_caller_rollback() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    record = CatalogRecord(
        id=str(uuid4()),
        kind="brand",
        source_id="raw/id",
        identity_key="brand:raw/id",
        source_baseline_json={},
        typed_fields_json={},
        extension_json={},
        raw_json={},
        first_seen_at=now,
        last_seen_at=now,
    )
    with Session(engine) as session:
        session.add(record)
        session.commit()
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=PNG, headers={"Content-Type": "image/png"}
                )
            )
        ) as client:
            result = fetch_logo(source(), client)
        staged = upsert_source_asset(session, record, source(), result)
        assert staged is not None and staged.blob == PNG
        session.rollback()
        assert session.scalar(select(Asset)) is None
    engine.dispose()
