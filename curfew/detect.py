"""Find running browsers."""
from __future__ import annotations

import ntpath

import psutil

from playlimiter.detect import _norm, _under


def main_processes(procs: dict[int, tuple[str, int | None]]) -> dict[int, str]:
    """Keep only each browser's main process: {pid: (exe, parent pid)} -> {pid: exe}.

    Chrome-style browsers run dozens of helper processes. Only the main one owns the
    windows, and the helpers exit with it, so only main processes are asked to close.
    A helper whose parent is gone counts as a main process, so it still gets closed.
    """
    return {pid: exe for pid, (exe, parent) in procs.items() if parent not in procs}


class BrowserScanner:
    def __init__(self):
        self._cache: dict[int, tuple[str | None, int | None]] = {}  # pid -> (exe, parent pid)
        self.names: set[str] = set()
        self.paths: list[str] = []

    def configure(self, browsers: list[str]) -> None:
        self.names = {b for b in browsers if "\\" not in b}
        self.paths = [_norm(b) for b in browsers if "\\" in b]

    def is_browser(self, exe: str) -> bool:
        exe = _norm(exe)
        return ntpath.basename(exe) in self.names or any(_under(exe, p) for p in self.paths)

    def scan(self) -> dict[int, str]:
        """Return {pid: exe} for running browsers' main processes. Only new pids are inspected."""
        pids = set(psutil.pids())
        for gone in self._cache.keys() - pids:
            del self._cache[gone]
        for pid in pids - self._cache.keys():
            try:
                p = psutil.Process(pid)
                self._cache[pid] = (p.exe() or None, p.ppid())
            except (psutil.Error, OSError):
                self._cache[pid] = (None, None)
        browsers = {pid: info for pid, info in self._cache.items() if info[0] and self.is_browser(info[0])}
        return main_processes(browsers)
