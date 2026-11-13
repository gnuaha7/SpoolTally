from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

from spooltally.db.models import Base, CatalogRun
from spooltally.db.session import create_engine, create_session_factory
from spooltally.ofd.scheduler import (
    claim_manual_run,
    execute_run,
    latest_due_slot,
    recover_abandoned_run,
    slot_for_day,
)


def test_latest_due_slot_uses_machine_zone_and_only_latest_date():
    zone = ZoneInfo("Europe/Madrid")
    now = datetime(2026, 11, 13, 1, 0, tzinfo=UTC)
    assert latest_due_slot(now, zone, "2026-11-11@03:00") == "2026-11-12@03:00"
    assert latest_due_slot(now, zone, "2026-11-12@03:00") is None


def test_manual_claim_is_single_flight_across_database_sessions(tmp_path):
    engine = create_engine(tmp_path / "runs.sqlite3")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    now = datetime(2026, 11, 13, 12, tzinfo=UTC)

    first = claim_manual_run(factory, now)
    second = claim_manual_run(factory, now)

    assert first.should_execute is True
    assert second.should_execute is False
    assert second.run_id == first.run_id
    with factory() as session:
        run = session.get(CatalogRun, first.run_id)
        assert run is not None
        assert (run.trigger, run.status, run.attempt_count) == ("manual", "running", 1)
    engine.dispose()


def test_scheduled_0300_gap_runs_at_first_valid_local_instant():
    zone = ZoneInfo("Europe/Helsinki")
    slot = slot_for_day(date(2026, 3, 29), zone)
    assert (slot.hour, slot.minute, slot.utcoffset()) == (
        4,
        0,
        datetime(2026, 3, 29, 4, tzinfo=zone).utcoffset(),
    )
    assert slot.astimezone(UTC) == datetime(2026, 3, 29, 1, tzinfo=UTC)


def test_repeated_scheduled_0300_has_one_local_date_key():
    zone = ZoneInfo("Europe/Helsinki")
    slot = slot_for_day(date(2026, 10, 25), zone)
    assert (slot.hour, slot.minute, slot.fold) == (3, 0, 0)
    assert slot.astimezone(UTC) == datetime(2026, 10, 25, 0, tzinfo=UTC)
    first = latest_due_slot(datetime(2026, 10, 25, 0, 30, tzinfo=UTC), zone, None)
    second = latest_due_slot(
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        zone,
        "2026-10-25@03:00",
    )
    assert first == "2026-10-25@03:00"
    assert second is None


def test_abandoned_run_reuses_identifier_and_increments_attempt_once(tmp_path):
    engine = create_engine(tmp_path / "recovery.sqlite3")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    run_id = "c08db55e-08e1-4f21-8a21-9ff397dbfbd8"
    started = datetime(2026, 11, 12, 3, tzinfo=UTC)
    with factory.begin() as session:
        session.add(
            CatalogRun(
                id=run_id,
                trigger="manual",
                schedule_key=None,
                started_at=started,
                status="running",
                attempt_count=1,
            )
        )
    claim = recover_abandoned_run(factory, datetime(2026, 11, 13, 4, tzinfo=UTC))
    assert claim is not None and claim.should_execute and claim.run_id == run_id
    with factory() as session:
        recovered = session.get(CatalogRun, run_id)
        assert recovered.attempt_count == 2
    engine.dispose()


def test_corrupt_gzip_marks_run_failed_before_catalog_changes(tmp_path):
    engine = create_engine(tmp_path / "corrupt.sqlite3")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    run_id = "8714b8f1-035e-48bf-9dc8-b92dd3027c38"
    now = datetime(2026, 11, 13, 12, tzinfo=UTC)
    with factory.begin() as session:
        session.add(
            CatalogRun(
                id=run_id,
                trigger="manual",
                schedule_key=None,
                started_at=now,
                status="running",
                attempt_count=1,
            )
        )

    def response(request):
        return httpx.Response(200, content=b"not a gzip stream")

    execute_run(
        factory,
        run_id,
        now=now,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(response)),
    )

    with factory() as session:
        run = session.get(CatalogRun, run_id)
        assert run.status == "failed"
        assert run.error_code == "invalid_gzip"
        assert run.source_digest is None
    engine.dispose()


def test_expanded_download_limit_accepts_exact_bytes_and_rejects_one_more():
    import gzip

    from spooltally.ofd.scheduler import MAX_EXPANDED, _bounded_source

    prefix = b'{"version":"v","filler":"'
    suffix = b'"}'

    def download(size):
        body = prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix
        encoded = gzip.compress(body)

        def response(request):
            return httpx.Response(200, content=encoded)

        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            return _bounded_source(client)

    expanded, _, version = download(MAX_EXPANDED)
    assert len(expanded) == MAX_EXPANDED
    assert version == "v"
    try:
        download(MAX_EXPANDED + 1)
    except ValueError as exc:
        assert str(exc) == "download_limit_exceeded"
    else:
        raise AssertionError("one byte over the expanded limit was accepted")


def test_compressed_download_limit_counts_exact_streamed_body_bytes():
    import gzip

    from spooltally.ofd.scheduler import MAX_COMPRESSED, _bounded_source

    prefix = b'{"version":"v"}'
    spaces = MAX_COMPRESSED - 2_000
    while True:
        encoded = gzip.compress(prefix + b" " * spaces, compresslevel=0)
        spaces += MAX_COMPRESSED - len(encoded)
        if len(encoded) == MAX_COMPRESSED:
            break

    def exact_response(request):
        return httpx.Response(200, content=encoded)

    with httpx.Client(transport=httpx.MockTransport(exact_response)) as client:
        expanded, compressed, version = _bounded_source(client)
    assert len(compressed) == MAX_COMPRESSED
    assert version == "v"
    assert len(expanded) <= 100_000_000

    encoded = gzip.compress(prefix + b" " * (spaces + 1), compresslevel=0)

    def over_response(request):
        return httpx.Response(200, content=encoded)

    with httpx.Client(transport=httpx.MockTransport(over_response)) as client:
        try:
            _bounded_source(client)
        except ValueError as exc:
            assert str(exc) == "download_limit_exceeded"
        else:
            raise AssertionError("one byte over the compressed limit was accepted")


def test_failed_scheduled_slot_is_not_claimed_twice(tmp_path):
    from spooltally.db.models import CatalogScheduleState
    from spooltally.ofd.scheduler import claim_due_run

    engine = create_engine(tmp_path / "schedule.sqlite3")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(
            CatalogScheduleState(
                id=1,
                timezone_name="Europe/Madrid",
                last_handled_slot_key="2026-11-12@03:00",
            )
        )
    now = datetime(2026, 11, 13, 12, tzinfo=UTC)
    first = claim_due_run(factory, now, ZoneInfo("Europe/Madrid"))
    assert first is not None and first.should_execute
    with factory.begin() as session:
        run = session.get(CatalogRun, first.run_id)
        run.status = "failed"
        run.finished_at = now
    assert claim_due_run(factory, now, ZoneInfo("Europe/Madrid")) is None
    engine.dispose()


def test_manual_request_reuses_a_scheduled_claim(tmp_path):
    from spooltally.db.models import CatalogScheduleState
    from spooltally.ofd.scheduler import claim_due_run

    engine = create_engine(tmp_path / "mixed.sqlite3")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(
            CatalogScheduleState(
                id=1,
                timezone_name="Europe/Madrid",
                last_handled_slot_key="2026-11-12@03:00",
            )
        )
    now = datetime(2026, 11, 13, 12, tzinfo=UTC)
    scheduled = claim_due_run(factory, now, ZoneInfo("Europe/Madrid"))
    manual = claim_manual_run(factory, now)
    assert scheduled is not None
    assert manual.run_id == scheduled.run_id
    assert not manual.should_execute
    engine.dispose()
