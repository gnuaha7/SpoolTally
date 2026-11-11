import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from spooltally.cli import CliError
from spooltally.db.models import TokenRecord, TokenScope

_TOKEN_TABLES = {"token_records", "token_scopes"}


def _invalid_input() -> CliError:
    return CliError("invalid_input", "Invalid token input.", 2)


def _schema_not_ready() -> CliError:
    return CliError("schema_not_ready", "Token schema is not ready.", 2)


def _ensure_schema(session: Session) -> None:
    if not _TOKEN_TABLES.issubset(inspect(session.get_bind()).get_table_names()):
        raise _schema_not_ready()


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
        .replace(".000000Z", "Z")
    )


def create_token(
    session_factory: sessionmaker[Session], name: str, role: str
) -> dict[str, str]:
    trimmed = name.strip()
    if role != "full_access" or not 1 <= len(trimmed) <= 80:
        raise _invalid_input()

    raw_bytes = secrets.token_bytes(32)
    raw_token = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    token_id = str(uuid.uuid4())
    created_at = datetime.now(UTC)
    record = TokenRecord(
        id=token_id,
        name=trimmed,
        token_hash=hashlib.sha256(raw_bytes).digest(),
        created_at=created_at,
    )
    with session_factory() as session:
        _ensure_schema(session)
        if session.scalar(select(TokenRecord.id).where(TokenRecord.name == trimmed)):
            raise _invalid_input()
        session.add(record)
        session.add(TokenScope(token_id=token_id, scope="full_access"))
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise _invalid_input() from exc

    return {
        "id": token_id,
        "name": trimmed,
        "role": "full_access",
        "created_at": _timestamp(created_at),
        "token": raw_token,
    }


def list_tokens(
    session_factory: sessionmaker[Session],
) -> dict[str, list[dict[str, str | None]]]:
    with session_factory() as session:
        _ensure_schema(session)
        records = sorted(
            session.scalars(select(TokenRecord)).all(),
            key=lambda record: (record.created_at, record.id),
        )
        return {
            "tokens": [
                {
                    "id": record.id,
                    "name": record.name,
                    "role": "full_access",
                    "created_at": _timestamp(record.created_at),
                    "revoked_at": (
                        None
                        if record.revoked_at is None
                        else _timestamp(record.revoked_at)
                    ),
                }
                for record in records
            ]
        }


def revoke_token(
    session_factory: sessionmaker[Session], token_id: str
) -> dict[str, str | bool]:
    try:
        parsed_id = uuid.UUID(token_id)
    except (ValueError, AttributeError):
        raise _invalid_input() from None
    if str(parsed_id) != token_id:
        raise _invalid_input()

    with session_factory() as session:
        _ensure_schema(session)
        record = session.get(TokenRecord, token_id)
        if record is None:
            raise CliError("token_not_found", "Token not found.", 3)
        changed = record.revoked_at is None
        if changed:
            record.revoked_at = datetime.now(UTC)
            session.commit()
        return {"id": token_id, "revoked": True, "changed": changed}
