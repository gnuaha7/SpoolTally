from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import re
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from spooltally.cli import CliError
from spooltally.db.base import _timezone_name
from spooltally.db.models import Asset, CatalogRecord, CatalogRun, CatalogScheduleState
from spooltally.ofd.ingest import ingest_catalog
from spooltally.ofd.logos import LogoSource, fetch_logo
from spooltally.ofd.schemas import SOURCE_SCHEMA_NAMES

BULK_URL = "https://api.openfilamentdatabase.org/json/all.json.gz"
MAX_COMPRESSED = 20_000_000
MAX_EXPANDED = 100_000_000
_SAFE_SCHEMA_FILE = re.compile(r"^[A-Za-z0-9_-]+\.json$")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunClaim:
    run_id: str
    should_execute: bool


def machine_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(_timezone_name())
    except (CliError, ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid_machine_timezone") from exc


def _valid_local(naive: datetime, zone: ZoneInfo) -> datetime | None:
    for fold in (0, 1):
        value = naive.replace(tzinfo=zone, fold=fold)
        if value.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive:
            return value
    return None


def slot_for_day(day: date, zone: ZoneInfo) -> datetime:
    target = datetime.combine(day, wall_time(3, 0))
    for minute in range(181):
        valid = _valid_local(target + timedelta(minutes=minute), zone)
        if valid is not None:
            return valid
    raise ValueError("unresolvable_schedule_slot")


def latest_due_slot(now: datetime, zone: ZoneInfo, last_key: str | None) -> str | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local_day = now.astimezone(zone).date()
    due = slot_for_day(local_day, zone)
    if now.astimezone(UTC) < due.astimezone(UTC):
        local_day -= timedelta(days=1)
    key = f"{local_day.isoformat()}@03:00"
    return key if last_key is None or key > last_key else None


def _active(session: Session) -> CatalogRun | None:
    return session.scalar(select(CatalogRun).where(CatalogRun.status == "running"))


def claim_manual_run(factory: sessionmaker[Session], now: datetime) -> RunClaim:
    with factory() as session:
        with session.begin():
            session.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
            active = _active(session)
            if active:
                return RunClaim(active.id, False)
            run_id = str(uuid4())
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
            return RunClaim(run_id, True)


def claim_due_run(
    factory: sessionmaker[Session], now: datetime, zone: ZoneInfo
) -> RunClaim | None:
    with factory() as session:
        with session.begin():
            session.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
            state = session.get(CatalogScheduleState, 1)
            if state is None:
                return None
            key = latest_due_slot(now, zone, state.last_handled_slot_key)
            if key is None or _active(session):
                return None
            run_id = str(uuid4())
            state.last_handled_slot_key = key
            session.add(
                CatalogRun(
                    id=run_id,
                    trigger="scheduled",
                    schedule_key=key,
                    started_at=now,
                    status="running",
                    attempt_count=1,
                )
            )
            return RunClaim(run_id, True)


def recover_abandoned_run(
    factory: sessionmaker[Session], now: datetime
) -> RunClaim | None:
    with factory() as session:
        with session.begin():
            session.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
            run = session.scalar(
                select(CatalogRun)
                .where(CatalogRun.status.in_(("running", "interrupted")))
                .order_by(CatalogRun.started_at)
            )
            if run is None:
                return None
            run.attempt_count += 1
            run.started_at = now
            run.status = "running"
            run.finished_at = None
            run.error_code = None
            return RunClaim(run.id, True)


def _ensure_running(stopped) -> None:
    if stopped is not None and stopped.is_set():
        raise ValueError("interrupted")


def _get_json(client: httpx.Client, url: str, stopped=None) -> bytes:
    _ensure_running(stopped)
    response = client.get(url)
    response.raise_for_status()
    return response.content


def _bounded_source(client: httpx.Client, stopped=None) -> tuple[bytes, bytes, str]:
    with client.stream("GET", BULK_URL) as response:
        response.raise_for_status()
        compressed = bytearray()
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        expanded = bytearray()
        try:
            for chunk in response.iter_raw():
                _ensure_running(stopped)
                compressed.extend(chunk)
                if len(compressed) > MAX_COMPRESSED:
                    raise ValueError("download_limit_exceeded")
                remaining = MAX_EXPANDED + 1 - len(expanded)
                expanded.extend(inflater.decompress(chunk, remaining))
                if len(expanded) > MAX_EXPANDED:
                    raise ValueError("download_limit_exceeded")
                while inflater.unconsumed_tail:
                    remaining = MAX_EXPANDED + 1 - len(expanded)
                    if remaining <= 0:
                        raise ValueError("download_limit_exceeded")
                    expanded.extend(
                        inflater.decompress(inflater.unconsumed_tail, remaining)
                    )
                    if len(expanded) > MAX_EXPANDED:
                        raise ValueError("download_limit_exceeded")
            expanded.extend(inflater.flush(max(1, MAX_EXPANDED + 1 - len(expanded))))
            if len(expanded) > MAX_EXPANDED:
                raise ValueError("download_limit_exceeded")
            if not inflater.eof or inflater.unused_data:
                raise ValueError("invalid_gzip")
        except zlib.error as exc:
            raise ValueError("invalid_gzip") from exc
    payload = json.loads(expanded)
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str):
        raise ValueError("invalid_catalog")
    return bytes(expanded), bytes(compressed), version


def _schema_documents(client: httpx.Client, stopped=None) -> dict[str, bytes]:
    root = "https://api.openfilamentdatabase.org/api/v1/schemas/"
    index = json.loads(_get_json(client, root + "index.json", stopped))
    if not isinstance(index, dict) or not isinstance(index.get("schemas"), list):
        raise ValueError("invalid_schema_index")
    names = set(SOURCE_SCHEMA_NAMES)
    for entry in index["schemas"]:
        if not isinstance(entry, dict):
            raise ValueError("invalid_schema_index")
        candidate = entry.get("path") or entry.get("file")
        if candidate is None:
            continue
        if not isinstance(candidate, str) or not _SAFE_SCHEMA_FILE.fullmatch(candidate):
            raise ValueError("invalid_schema_index")
        names.add(candidate.removesuffix(".json"))
    return {
        name: _get_json(client, root + name + ".json", stopped)
        for name in sorted(names)
    }


def _logo_sources(
    client: httpx.Client, payload: dict[str, Any], stopped=None
) -> list[LogoSource]:
    result: list[LogoSource] = []
    for kind, collection in (("brand", "brands"), ("store", "stores")):
        try:
            index_data = json.loads(
                _get_json(
                    client,
                    f"https://api.openfilamentdatabase.org/api/v1/{collection}/logo/index.json",
                    stopped,
                )
            )
        except (httpx.HTTPError, ValueError):
            continue
        if not isinstance(index_data, dict) or not isinstance(
            index_data.get("logos"), list
        ):
            continue
        entries = index_data["logos"]
        incoming = {
            str(row.get("id"))
            for row in payload.get(collection, [])
            if isinstance(row, dict)
        }
        owner_key = "brand_id" if kind == "brand" else "store_id"
        for entry in entries:
            if not isinstance(entry, dict) or str(entry.get(owner_key)) not in incoming:
                continue
            logo_id = entry.get("id")
            path = entry.get("path")
            if (
                not isinstance(logo_id, str)
                or not logo_id
                or path != f"{logo_id}.json"
            ):
                continue
            try:
                metadata = json.loads(
                    _get_json(
                        client,
                        "https://api.openfilamentdatabase.org/api/v1/"
                        f"{collection}/logo/{quote(logo_id, safe='')}.json",
                        stopped,
                    )
                )
                result.append(
                    LogoSource(
                        cast(Literal["brand", "store"], kind),
                        str(entry[owner_key]),
                        entry,
                        metadata,
                    )
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                continue
    return result


def execute_run(
    factory: sessionmaker[Session],
    run_id: str,
    *,
    now: datetime,
    client_factory=None,
    stopped=None,
) -> None:
    client_factory = client_factory or (
        lambda: httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=False)
    )
    try:
        with client_factory() as client:
            if stopped and stopped.is_set():
                raise ValueError("interrupted")
            source, _compressed, version = _bounded_source(client, stopped)
            payload = json.loads(source)
            schemas = _schema_documents(client, stopped)
            logo_work = []
            for logo in _logo_sources(client, payload, stopped):
                _ensure_running(stopped)
                with factory() as session:
                    owner = session.scalar(
                        select(CatalogRecord).where(
                            CatalogRecord.kind == logo.kind,
                            CatalogRecord.source_id == logo.source_id,
                        )
                    )
                    prior = (
                        session.scalar(
                            select(Asset).where(Asset.source_record_id == owner.id)
                        )
                        if owner
                        else None
                    )
                    owner_id = owner.id if owner else None
                logo_work.append((logo, fetch_logo(logo, client, prior, owner_id)))
        if stopped and stopped.is_set():
            raise ValueError("interrupted")
        with factory() as session:
            with session.begin():
                session.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
                ingest_catalog(
                    session, source, schemas, fetched_at=now, logos=logo_work
                )
                run = session.get(CatalogRun, run_id)
                if run is None or run.status != "running":
                    raise ValueError("run_not_active")
                run.status, run.finished_at = "succeeded", now
                run.source_version = version
                run.source_digest = hashlib.sha256(source).digest()
    except Exception as exc:
        code = (
            str(exc)
            if str(exc)
            in {
                "download_limit_exceeded",
                "invalid_gzip",
                "invalid_catalog",
                "interrupted",
            }
            else "refresh_failed"
        )
        with factory() as session:
            with session.begin():
                run = session.get(CatalogRun, run_id)
                if run is not None and run.status == "running":
                    if code == "interrupted":
                        run.status = "interrupted"
                        run.finished_at = None
                        run.error_code = code
                    else:
                        run.status, run.finished_at, run.error_code = (
                            "failed",
                            now,
                            code,
                        )


async def _poll(app, stop: asyncio.Event, lock_file):
    factory = app.state.session_factory
    try:
        zone = machine_timezone()
    except ValueError as exc:
        logger.error("Catalog scheduler unavailable: %s", exc)
        return
    while not stop.is_set():
        now = app.state.clock()
        claim = claim_due_run(factory, now, zone)
        if claim and claim.should_execute:
            task = asyncio.create_task(
                asyncio.to_thread(
                    execute_run,
                    factory,
                    claim.run_id,
                    now=now,
                    client_factory=getattr(app.state, "client_factory", None),
                    stopped=stop,
                )
            )
            app.state.refresh_worker = task
            await asyncio.shield(task)
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except TimeoutError:
            pass


def router_lifespan(router):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        database_path = app.state.settings.database_path
        if not database_path.exists():
            yield
            return
        stop = asyncio.Event()
        app.state.clock = getattr(app.state, "clock", lambda: datetime.now(UTC))
        app.state.refresh_owner = False
        app.state.refresh_stop = stop
        lock_path = Path(str(app.state.settings.database_path) + ".refresh.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            app.state.refresh_owner = True
            try:
                with app.state.engine.connect() as conn:
                    present = conn.execute(
                        text("SELECT 1 FROM catalog_schedule_state WHERE id=1")
                    ).scalar_one_or_none()
            except SQLAlchemyError:
                present = None
            if present:
                now = app.state.clock()
                recovered = recover_abandoned_run(app.state.session_factory, now)
                if recovered is not None:
                    app.state.refresh_worker = asyncio.create_task(
                        asyncio.to_thread(
                            execute_run,
                            app.state.session_factory,
                            recovered.run_id,
                            now=now,
                            client_factory=getattr(app.state, "client_factory", None),
                            stopped=stop,
                        )
                    )
                task = asyncio.create_task(_poll(app, stop, lock_file))
                app.state.refresh_poller = task
            else:
                task = None
            try:
                yield
            finally:
                stop.set()
                if task:
                    await asyncio.shield(task)
                worker = getattr(app.state, "refresh_worker", None)
                if worker:
                    await asyncio.shield(worker)
        except BlockingIOError:
            yield
        finally:
            app.state.refresh_owner = False
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            finally:
                lock_file.close()

    return lifespan
