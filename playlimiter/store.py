"""File locations and atomic persistence."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "PlayLimiter"


def config_path() -> Path:
    return data_dir() / "config.toml"


def state_path() -> Path:
    return data_dir() / "state.json"


def log_path() -> Path:
    return data_dir() / "playlimiter.log"


def requests_dir() -> Path:
    return data_dir() / "requests"


EXAMPLE_CONFIG = Path(__file__).with_name("config.example.toml")


def ensure_config() -> bool:
    """Create config.toml from config.example.toml if missing. Returns True if created."""
    data_dir().mkdir(parents=True, exist_ok=True)
    if config_path().exists():
        return False
    shutil.copyfile(EXAMPLE_CONFIG, config_path())
    return True


def _retry(fn, attempts=20, delay=0.05):
    # On Windows, replace/read can briefly fail while another process has the file open.
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    _retry(lambda: os.replace(tmp, path))


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None

    def _read():
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    return _retry(_read)
