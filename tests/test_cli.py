import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from spooltally.cli import CliError, main
from spooltally.config import Settings, load_settings


@pytest.fixture
def invalid_data_dirs(tmp_path: Path) -> dict[str, str]:
    regular_file = tmp_path / "regular-file"
    regular_file.write_text("not a directory")
    symlink_loop = tmp_path / "symlink-loop"
    symlink_loop.symlink_to(symlink_loop)
    return {
        "unknown_home_user": "~spooltally_missing_user_97431/data",
        "data_dir_is_file": str(regular_file),
        "data_dir_below_file": str(regular_file / "data"),
        "data_dir_symlink_loop": str(symlink_loop),
    }


@pytest.mark.parametrize("command", (("init",), ("tokens", "list"), ("serve",)))
@pytest.mark.parametrize(
    "input_class",
    (
        "unknown_home_user",
        "data_dir_is_file",
        "data_dir_below_file",
        "data_dir_symlink_loop",
    ),
)
def test_invalid_data_directory_is_safe_invalid_input(
    run_cli,
    invalid_data_dirs: dict[str, str],
    command: tuple[str, ...],
    input_class: str,
) -> None:
    path = invalid_data_dirs[input_class]
    result = run_cli(*command, "--data-dir", path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        '{"error":{"code":"invalid_input","message":"Invalid configuration."}}\n'
    )
    assert path not in result.stderr


def test_help_exits_before_data_access(run_cli) -> None:
    result = run_cli("serve", "--help")

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    assert result.stderr == ""


def test_init_reports_schema_current_and_creates_sqlite_file(
    run_cli, tmp_path: Path
) -> None:
    data_dir = tmp_path / "new" / "nested" / "local data"

    result = run_cli("init", "--data-dir", str(data_dir))

    assert result.returncode == 0
    assert (
        result.stdout
        == json.dumps(
            {
                "data_dir": str(data_dir.resolve()),
                "database_path": str((data_dir / "spooltally.sqlite3").resolve()),
                "schema_status": "current",
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    assert result.stderr == ""
    assert (data_dir / "spooltally.sqlite3").is_file()
    assert (data_dir.stat().st_mode & 0o777) == 0o700
    assert ((data_dir / "spooltally.sqlite3").stat().st_mode & 0o777) == 0o600


def test_init_ignores_invalid_server_environment(run_cli, tmp_path: Path) -> None:
    data_dir = tmp_path / "offline"
    env = os.environ.copy()
    env["SPOOLTALLY_HOST"] = "example.invalid"
    env["SPOOLTALLY_PORT"] = "not-a-port"

    result = run_cli("init", "--data-dir", str(data_dir), env=env)

    assert result.returncode == 0
    assert result.stdout == json.dumps(
        {
            "data_dir": str(data_dir.resolve()),
            "database_path": str((data_dir / "spooltally.sqlite3").resolve()),
            "schema_status": "current",
        },
        separators=(",", ":"),
    ) + "\n"
    assert result.stderr == ""
    assert (data_dir / "spooltally.sqlite3").is_file()


def test_serve_occupied_port_returns_safe_startup_error(
    run_cli, tmp_path: Path
) -> None:
    data_dir = tmp_path / "initialized"
    initialized = run_cli("init", "--data-dir", str(data_dir))
    assert initialized.returncode == 0

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = run_cli(
            "serve",
            "--data-dir",
            str(data_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        '{"error":{"code":"startup_failed",'
        '"message":"SpoolTally could not start"}}\n'
    )


def test_serve_missing_database_returns_safe_error(run_cli, tmp_path: Path) -> None:
    result = run_cli("serve", "--data-dir", str(tmp_path / "missing"))

    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "error": {"code": "data_not_initialized", "message": "Data is not initialized."}
    }


def test_invalid_serve_configuration_exits_before_startup(
    run_cli, tmp_path: Path
) -> None:
    result = run_cli("serve", "--data-dir", str(tmp_path), "--host", "example.invalid")

    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "error": {"code": "invalid_input", "message": "Invalid configuration."}
    }


def test_token_command_reports_schema_not_ready_before_core04(
    run_cli, tmp_path: Path
) -> None:
    (tmp_path / "spooltally.sqlite3").touch()

    result = run_cli(
        "tokens", "create", "--name", "printer", "--data-dir", str(tmp_path)
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "error": {"code": "schema_not_ready", "message": "Token schema is not ready."}
    }


def test_settings_resolve_cli_environment_and_default_paths(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPOOLTALLY_DATA_DIR", "~/spooltally-env")
    monkeypatch.setenv("SPOOLTALLY_HOST", "localhost")
    monkeypatch.setenv("SPOOLTALLY_PORT", "8123")

    configured = load_settings(
        data_dir=str(tmp_path / "cli"), host="127.0.0.2", port="9000"
    )

    assert configured == Settings(tmp_path / "cli", "127.0.0.2", 9000)
    assert load_settings() == Settings(
        Path.home() / "spooltally-env", "localhost", 8123
    )


def test_settings_reject_invalid_host_and_non_ascii_port(monkeypatch) -> None:
    monkeypatch.delenv("SPOOLTALLY_DATA_DIR", raising=False)
    monkeypatch.delenv("SPOOLTALLY_HOST", raising=False)
    monkeypatch.delenv("SPOOLTALLY_PORT", raising=False)

    for options in (
        {"host": "spooltally.invalid"},
        {"port": "８０００"},
        {"port": "65536"},
    ):
        try:
            load_settings(**options)
        except ValueError as exc:
            assert str(exc) == "Invalid configuration."
        else:
            raise AssertionError(f"accepted invalid settings: {options}")


def test_tokens_command_preserves_expected_handler_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    database_path = tmp_path / "spooltally.sqlite3"
    database_path.touch()
    token_module = ModuleType("spooltally.tokens")

    def revoke_token(session_factory, token_id) -> None:
        raise CliError("token_not_found", "Token not found.", 3)

    token_module.revoke_token = revoke_token
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        "spooltally.cli.importlib.import_module", lambda name: token_module
    )

    exit_code = main(
        [
            "tokens",
            "revoke",
            "00000000-0000-4000-8000-000000000001",
            "--data-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "token_not_found", "message": "Token not found."}
    }


def test_serve_writes_no_success_json_and_healthz_reports_ready(
    run_cli, tmp_path: Path
) -> None:
    data_dir = tmp_path / "selected"
    wrong_dir = tmp_path / "from-env"
    init = run_cli("init", "--data-dir", str(data_dir))
    assert init.returncode == 0

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    env = os.environ.copy()
    env["SPOOLTALLY_DATA_DIR"] = str(wrong_dir)
    env["SPOOLTALLY_HOST"] = "example.invalid"
    env["SPOOLTALLY_PORT"] = "not-a-port"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "spooltally",
            "serve",
            "--data-dir",
            str(data_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        response = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"server exited before readiness with {process.returncode}"
                )
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5)
                break
            except httpx.ConnectError:
                time.sleep(0.05)
        assert response is not None
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
        assert not wrong_dir.exists()
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)

    assert stdout == ""
    assert "Uvicorn running" in stderr
