"""Close games: WM_CLOSE first, force-terminate only after the grace period."""
from __future__ import annotations

import logging

import psutil
import win32con
import win32gui
import win32process

from .detect import still_same_process

log = logging.getLogger(__name__)


def post_close(pid: int) -> int:
    """Post WM_CLOSE to every top-level window owned by pid. Returns windows signalled."""
    hwnds = []

    def cb(hwnd, _):
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
                hwnds.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    for hwnd in hwnds:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as e:
            log.debug("PostMessage failed for %s: %s", hwnd, e)
    return len(hwnds)


class Closer:
    """Tracks processes that were asked to close and kills them after grace_seconds."""

    def __init__(self):
        # pid -> [exe, kill_at mono, window got WM_CLOSE]
        self._closing: dict[int, list] = {}

    def is_closing(self, pid: int) -> bool:
        return pid in self._closing

    def close(self, pid: int, exe: str, now: float, grace_s: float) -> None:
        if pid in self._closing:
            return
        n = post_close(pid)
        log.info("Asked %s (pid %d) to close via %d window(s); force close in %ss", exe, pid, n, grace_s)
        self._closing[pid] = [exe, now + grace_s, n > 0]

    def step(self, now: float) -> None:
        for pid, entry in list(self._closing.items()):
            exe, kill_at, signalled = entry
            proc = still_same_process(pid, exe)
            if proc is None:
                del self._closing[pid]
                continue
            if not signalled:
                # A game that was just launched may not have a window yet. Keep trying until one
                # gets WM_CLOSE, but only once: repeating it could dismiss a "save before quitting?" dialog.
                n = post_close(pid)
                if n:
                    entry[2] = True
                    log.info("Asked %s (pid %d) to close via %d window(s)", exe, pid, n)
            if now >= kill_at:
                try:
                    proc.kill()
                    log.warning("Force-closed %s (pid %d) after grace period", exe, pid)
                except psutil.Error as e:
                    log.error("Could not terminate %s (pid %d): %s", exe, pid, e)
                del self._closing[pid]
