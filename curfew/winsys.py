"""Browser Curfew's logon task, mutex and desktop shortcut (on top of playlimiter.winsys)."""
from __future__ import annotations

from pathlib import Path

from playlimiter import winsys

from . import store

TASK_NAME = "BrowserCurfew"
SHORTCUT_NAME = "Browser Curfew.lnk"
ICON_PATH = Path(__file__).with_name("icon.ico")


def _mutex() -> str:
    return winsys.mutex_name(store.APP, store.data_dir())


def acquire_single_instance():
    return winsys.acquire_single_instance(_mutex())


def daemon_running() -> bool:
    return winsys.daemon_running(_mutex())


def register_task() -> None:
    winsys.register_task(store.data_dir(), TASK_NAME, "curfew", "Browser Curfew: closes browsers at night.")


def run_task() -> None:
    winsys.run_task(TASK_NAME)


def delete_task() -> bool:
    return winsys.delete_task(TASK_NAME)


def task_exists() -> bool:
    return winsys.task_exists(TASK_NAME)


def shortcut_path() -> Path:
    return winsys.desktop_dir() / SHORTCUT_NAME


def create_shortcut() -> Path:
    return winsys.make_shortcut(shortcut_path(), "curfew", ICON_PATH, "Change Browser Curfew settings")


def remove_shortcut() -> None:
    winsys.remove_shortcut(shortcut_path())
