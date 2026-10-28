"""Find running browsers, chat apps and games."""
from __future__ import annotations

import ntpath

import psutil

from playlimiter.detect import GameScanner, _norm, _under


def main_processes(procs: dict[int, tuple[str, int | None]]) -> dict[int, str]:
    """Keep only each app's main process: {pid: (exe, parent pid)} -> {pid: exe}.

    Chrome-style browsers (and Discord, Teams) run dozens of helper processes. Only the main
    one owns the windows, and the helpers exit with it, so only main processes are asked to close.
    A helper whose parent is gone counts as a main process, so it still gets closed.
    """
    return {pid: exe for pid, (exe, parent) in procs.items() if parent not in procs}


class AppScanner:
    def __init__(self):
        self._cache: dict[int, tuple[str | None, int | None]] = {}  # pid -> (exe, parent pid)
        self.names: set[str] = set()
        self.paths: list[str] = []
        self.games: GameScanner | None = None  # set when close_games is on

    def configure(self, apps: list[str], close_games: bool) -> None:
        self.names = {b for b in apps if "\\" not in b}
        self.paths = [_norm(b) for b in apps if "\\" in b]
        if close_games and self.games is None:
            self.games = GameScanner()  # Steam libraries, found the same way as PlayLimiter
        elif not close_games:
            self.games = None

    def is_target(self, exe: str) -> bool:
        norm = _norm(exe)
        if ntpath.basename(norm) in self.names or any(_under(norm, p) for p in self.paths):
            return True
        return self.games is not None and self.games.is_game(exe)

    def scan(self) -> dict[int, str]:
        """Return {pid: exe} for the target apps' main processes. Only new pids are inspected."""
        pids = set(psutil.pids())
        for gone in self._cache.keys() - pids:
            del self._cache[gone]
        for pid in pids - self._cache.keys():
            try:
                p = psutil.Process(pid)
                self._cache[pid] = (p.exe() or None, p.ppid())
            except (psutil.Error, OSError):
                self._cache[pid] = (None, None)
        targets = {pid: info for pid, info in self._cache.items() if info[0] and self.is_target(info[0])}
        return main_processes(targets)
