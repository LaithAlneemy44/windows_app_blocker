"""Grace given to apps the curfew daemon closes (no real processes, toasts or banners)."""
from datetime import datetime

import pytest

from curfew import daemon, rules


class FakeCloser:
    def __init__(self):
        self.closed = {}  # pid -> grace seconds

    def is_closing(self, pid):
        return pid in self.closed

    def close(self, pid, path, mono, grace):
        self.closed[pid] = grace

    def step(self, mono):
        pass


class FakeScanner:
    def configure(self, apps, close_games):
        pass


@pytest.fixture
def d(monkeypatch):
    monkeypatch.setattr(daemon, "toast", lambda *a, **k: None)
    monkeypatch.setattr(daemon.overlay, "show_async", lambda *a, **k: None)
    monkeypatch.setattr(daemon.pl_winsys, "uptime_s", lambda: 1000.0)
    obj = object.__new__(daemon.Daemon)
    obj.state = rules.State(active=dict(rules.DEFAULTS))
    obj.scanner = FakeScanner()
    obj.closer = FakeCloser()
    obj.last_blocked_notice = -daemon.BLOCKED_NOTICE_EVERY_S
    obj.first_scan = True
    return obj


def blocked(just_blocked=False):
    now = datetime(2026, 11, 7, 22, 34)
    status = rules.Status(
        blocked=True, day=1, bedtime="22:30", changed=False, starts_at=now, ends_at=now,
        deadline=now, change_used=False,
    )
    return rules.TickResult(status, just_blocked=just_blocked)


def test_apps_open_when_daemon_starts_mid_curfew_get_full_grace(d):
    d.handle_tick(blocked(), {1: "chrome.exe"})
    assert d.closer.closed[1] == rules.DEFAULTS["grace_minutes"] * 60


def test_app_launched_later_in_the_curfew_gets_short_grace(d):
    d.handle_tick(blocked(), {})
    d.handle_tick(blocked(), {2: "chrome.exe"})
    assert d.closer.closed[2] == daemon.BLOCKED_LAUNCH_GRACE_S


def test_curfew_starting_gives_full_grace(d):
    d.first_scan = False
    d.handle_tick(blocked(just_blocked=True), {3: "chrome.exe"})
    assert d.closer.closed[3] == rules.DEFAULTS["grace_minutes"] * 60
