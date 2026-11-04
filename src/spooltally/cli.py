import argparse
import importlib
import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from copy import deepcopy
from typing import Never

from spooltally.config import Settings, load_settings


class CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CliError("invalid_input", "Invalid input.", 2)


def _parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(prog="spooltally")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init")
    init_parser.add_argument("--data-dir")

    tokens_parser = commands.add_parser("tokens")
    token_commands = tokens_parser.add_subparsers(dest="token_command", required=True)
    create_parser = token_commands.add_parser("create")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--role", default="full_access")
    create_parser.add_argument("--data-dir")
    list_parser = token_commands.add_parser("list")
    list_parser.add_argument("--data-dir")
    revoke_parser = token_commands.add_parser("revoke")
    revoke_parser.add_argument("token_id")
    revoke_parser.add_argument("--data-dir")

    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--data-dir")
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port")
    return parser


def _error(error: CliError) -> int:
    sys.stderr.write(
        json.dumps(
            {"error": {"code": error.code, "message": str(error)}},
            separators=(",", ":"),
        )
        + "\n"
    )
    return error.exit_code


def _settings(args: argparse.Namespace) -> Settings:
    try:
        return load_settings(
            data_dir=getattr(args, "data_dir", None),
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
        )
    except (OSError, ValueError) as exc:
        raise CliError("invalid_input", "Invalid configuration.", 2) from exc


def _init(settings: Settings) -> int:
    from spooltally.db.session import create_engine

    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    database_created = not settings.database_path.exists()
    engine = create_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1").scalar_one()
    except Exception as exc:
        engine.dispose()
        if isinstance(exc, CliError):
            raise
        raise CliError("startup_failed", "SpoolTally could not start", 1) from exc

    if database_created:
        settings.database_path.chmod(0o600)

    try:
        base_module = importlib.import_module("spooltally.db.base")
    except ModuleNotFoundError as exc:
        if exc.name != "spooltally.db.base":
            engine.dispose()
            raise CliError("startup_failed", "SpoolTally could not start", 1) from exc
        schema_status = "pending"
    else:
        try:
            base_module.initialize_database(settings)
        except Exception as exc:
            engine.dispose()
            if isinstance(exc, CliError):
                raise
            raise CliError("startup_failed", "SpoolTally could not start", 1) from exc
        schema_status = "current"
    finally:
        engine.dispose()

    sys.stdout.write(
        json.dumps(
            {
                "data_dir": str(settings.data_dir),
                "database_path": str(settings.database_path),
                "schema_status": schema_status,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


def _tokens(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.database_path.is_file():
        raise CliError("data_not_initialized", "Data is not initialized.", 2)
    if importlib.util.find_spec("spooltally.tokens") is None:
        raise CliError("schema_not_ready", "Token schema is not ready.", 2)
    tokens = importlib.import_module("spooltally.tokens")

    from spooltally.db.session import create_engine, create_session_factory

    engine = create_engine(settings.database_path)
    factory = create_session_factory(engine)
    try:
        if args.token_command == "create":
            result = tokens.create_token(factory, args.name, args.role)
        elif args.token_command == "list":
            result = tokens.list_tokens(factory)
        else:
            result = tokens.revoke_token(factory, args.token_id)
    except Exception as exc:
        if isinstance(exc, CliError):
            raise
        raise CliError("startup_failed", "SpoolTally could not start", 1) from exc
    finally:
        engine.dispose()
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        settings = _settings(args)
        if args.command == "init":
            return _init(settings)
        if args.command == "tokens":
            return _tokens(args, settings)
        if not settings.database_path.is_file():
            raise CliError("data_not_initialized", "Data is not initialized.", 2)

        import uvicorn

        os.environ["SPOOLTALLY_DATA_DIR"] = str(settings.data_dir)
        os.environ["SPOOLTALLY_HOST"] = settings.host
        os.environ["SPOOLTALLY_PORT"] = str(settings.port)
        log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
        log_config["handlers"]["default"]["stream"] = "ext://sys.stderr"
        log_config["handlers"]["access"]["stream"] = "ext://sys.stderr"
        uvicorn.run(
            "spooltally.api.app:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            log_config=log_config,
        )
        return 0
    except CliError as exc:
        return _error(exc)
    except Exception:
        return _error(CliError("startup_failed", "SpoolTally could not start", 1))
