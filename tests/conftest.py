import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run_cli():
    def run(
        *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        child_env = os.environ.copy()
        child_env.pop("SPOOLTALLY_DATA_DIR", None)
        child_env.pop("SPOOLTALLY_HOST", None)
        child_env.pop("SPOOLTALLY_PORT", None)
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "spooltally", *args],
            cwd=ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
        )

    return run
