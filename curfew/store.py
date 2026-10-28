"""Browser Curfew's file locations. Kept apart from PlayLimiter's data folder."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from playlimiter.store import read_json, write_json_atomic, write_text_atomic  # noqa: F401 (re-exported)

APP = "BrowserCurfew"


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP


def config_path() -> Path:
    return data_dir() / "config.toml"


def state_path() -> Path:
    return data_dir() / "state.json"


def log_path() -> Path:
    return data_dir() / "curfew.log"


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
