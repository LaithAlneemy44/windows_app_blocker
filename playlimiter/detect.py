"""Find running game processes."""
from __future__ import annotations

import ntpath
import os
import time

import psutil

from .steam import steam_common_dirs

LIBRARY_REFRESH_S = 300
# Steam's redistributable installers live under steamapps\common but aren't games.
ALWAYS_IGNORED_DIRS = ("steamworks shared",)


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(os.path.expandvars(path)))


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("\\") + "\\")


class GameScanner:
    def __init__(self):
        self._exe_cache: dict[int, str | None] = {}
        self._steam_roots: list[str] = []
        self._roots_at = -LIBRARY_REFRESH_S
        self.exclude: list[str] = []
        self.extra_paths: list[str] = []

    def configure(self, exclude: list[str], extra_paths: list[str]) -> None:
        self.exclude = list(exclude)
        self.extra_paths = [_norm(p) for p in extra_paths]

    def _roots(self) -> list[str]:
        now = time.monotonic()
        if now - self._roots_at >= LIBRARY_REFRESH_S:
            self._steam_roots = [_norm(str(p)) for p in steam_common_dirs()]
            self._roots_at = now
        return self._steam_roots + self.extra_paths

    def is_game(self, exe: str) -> bool:
        exe = _norm(exe)
        roots = self._roots()
        root = next((r for r in roots if _under(exe, r)), None)
        if root is None:
            return False
        if root in self._steam_roots:
            first = exe[len(root):].lstrip("\\").split("\\", 1)[0]
            if first in ALWAYS_IGNORED_DIRS:
                return False
        name = ntpath.basename(exe)
        for entry in self.exclude:
            if "\\" in entry:
                if _under(exe, _norm(entry)):
                    return False
            elif name == entry:
                return False
        return True

    def scan(self) -> dict[int, str]:
        """Return {pid: exe} for running game processes. Only new pids are inspected."""
        pids = set(psutil.pids())
        for gone in self._exe_cache.keys() - pids:
            del self._exe_cache[gone]
        for pid in pids - self._exe_cache.keys():
            try:
                self._exe_cache[pid] = psutil.Process(pid).exe() or None
            except (psutil.Error, OSError):
                self._exe_cache[pid] = None
        return {pid: exe for pid, exe in self._exe_cache.items() if exe and self.is_game(exe)}


def still_same_process(pid: int, exe: str) -> psutil.Process | None:
    """Re-check a pid before acting on it, in case it was reused."""
    try:
        p = psutil.Process(pid)
        return p if _norm(p.exe()) == _norm(exe) else None
    except (psutil.Error, OSError):
        return None
