import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int

    @property
    def database_path(self) -> Path:
        return self.data_dir / "spooltally.sqlite3"


def load_settings(
    data_dir: str | None = None,
    host: str | None = None,
    port: str | None = None,
    include_server: bool = True,
) -> Settings:
    configured_data_dir = (
        data_dir
        if data_dir is not None
        else os.environ.get("SPOOLTALLY_DATA_DIR", "~/.local/share/spooltally")
    )
    resolved_data_dir = Path(configured_data_dir).expanduser().resolve()
    if include_server:
        resolved_host = (
            host if host is not None else os.environ.get("SPOOLTALLY_HOST", "127.0.0.1")
        )
        resolved_port = (
            port if port is not None else os.environ.get("SPOOLTALLY_PORT", "8000")
        )
    else:
        resolved_host = host if host is not None else "127.0.0.1"
        resolved_port = port if port is not None else "8000"

    if resolved_host != "localhost":
        try:
            ipaddress.ip_address(resolved_host)
        except ValueError as exc:
            raise ValueError("Invalid configuration.") from exc

    if not resolved_port.isascii() or not resolved_port.isdecimal():
        raise ValueError("Invalid configuration.")
    port_number = int(resolved_port)
    if not 1 <= port_number <= 65535:
        raise ValueError("Invalid configuration.")

    return Settings(resolved_data_dir, resolved_host, port_number)
