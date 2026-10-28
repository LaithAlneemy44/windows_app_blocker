import copy
from datetime import datetime, timedelta

import pytest

from curfew import rules
from curfew.config import parse_config, parse_time, render_config
from curfew.detect import main_processes
from curfew.rules import DEFAULTS, CurfewError, Engine, State
from playlimiter import rules as pl_rules
from playlimiter.config import ConfigError

HOUR = 3600


def at(day, hhmm):
    """Local time on 2026-01-<day>."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 1, day, h, m)


def make(**overrides):
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(overrides)
    return State(active=cfg)


# ---------------------------------------------------------------- the nightly window

def test_blocked_from_bedtime_until_wake():
    state = make()
    assert not rules.evaluate(state, at(5, "22:29")).blocked
    assert rules.evaluate(state, at(5, "22:30")).blocked
    assert rules.evaluate(state, at(6, "02:00")).blocked
    assert rules.evaluate(state, at(6, "06:59")).blocked
    assert not rules.evaluate(state, at(6, "07:00")).blocked


def test_after_midnight_belongs_to_the_evening_before():
    st = rules.evaluate(make(), at(6, "01:00"))
    assert st.starts_at == at(5, "22:30")
    assert st.ends_at == at(6, "07:00")


def test_bedtime_after_midnight():
    state = make(bedtime="00:30")
    assert not rules.evaluate(state, at(5, "23:59")).blocked
    assert rules.evaluate(state, at(6, "00:30")).blocked


# ---------------------------------------------------------------- tonight only

def test_later_once_before_deadline_then_back_to_default_next_day():
    state = make()
    msg = rules.set_tonight(state, at(5, "15:00"), "23:30")
    assert "23:30" in msg
    assert not rules.evaluate(state, at(5, "23:00")).blocked
    assert rules.evaluate(state, at(5, "23:30")).blocked
    # Next day: default again.
    st = rules.evaluate(state, at(6, "12:00"))
    assert st.bedtime == "22:30" and not st.changed
    assert rules.evaluate(state, at(6, "22:30")).blocked


def test_later_refused_after_deadline():
    state = make()
    with pytest.raises(CurfewError, match="16:00"):
        rules.set_tonight(state, at(5, "16:00"), "23:30")
    assert rules.evaluate(state, at(5, "22:30")).blocked


def test_only_one_later_change_per_day():
    state = make()
    rules.set_tonight(state, at(5, "09:00"), "23:00")
    with pytest.raises(CurfewError, match="already used"):
        rules.set_tonight(state, at(5, "10:00"), "23:30")
    # The next day has its own change.
    rules.set_tonight(state, at(6, "09:00"), "23:30")
    assert rules.evaluate(state, at(6, "12:00")).bedtime == "23:30"


def test_earlier_allowed_any_time_and_does_not_use_the_change():
    state = make()
    rules.set_tonight(state, at(5, "20:00"), "21:00")  # after the deadline
    assert rules.evaluate(state, at(5, "21:00")).blocked
    state = make()
    rules.set_tonight(state, at(5, "10:00"), "21:00")
    rules.set_tonight(state, at(5, "11:00"), "23:00")  # still has today's change
    assert rules.evaluate(state, at(5, "22:59")).bedtime == "23:00"


def test_going_back_up_to_default_counts_as_later():
    state = make()
    rules.set_tonight(state, at(5, "17:00"), "21:00")
    with pytest.raises(CurfewError):
        rules.set_tonight(state, at(5, "17:30"), "22:30")


def test_cannot_make_later_once_curfew_started():
    state = make(bedtime="15:00", change_deadline="16:00")
    with pytest.raises(CurfewError, match="already started"):
        rules.set_tonight(state, at(5, "15:30"), "23:00")


def test_set_tonight_after_midnight_is_later():
    state = make()
    rules.set_tonight(state, at(5, "12:00"), "01:00")
    assert not rules.evaluate(state, at(6, "00:30")).blocked
    assert rules.evaluate(state, at(6, "01:00")).blocked


def test_tonight_cannot_equal_wake_time():
    with pytest.raises(CurfewError):
        rules.set_tonight(make(), at(5, "12:00"), "07:00")


# ---------------------------------------------------------------- ticking

class Clock:
    def __init__(self, start):
        self.local, self.wall, self.mono = start, 1_000_000.0, 500.0

    def advance(self, seconds):
        self.local += timedelta(seconds=seconds)
        self.wall += seconds
        self.mono += seconds


def run(engine, clock, seconds, in_use, step=2):
    out = []
    for _ in range(int(seconds // step)):
        clock.advance(step)
        out.append(engine.tick(clock.wall, clock.mono, clock.local, in_use))
    return out


def test_just_blocked_once_and_warning_before():
    state = make(warning_minutes=[15, 5])
    clock = Clock(at(5, "22:00"))
    engine = Engine(state)
    engine.start(clock.wall, clock.mono, clock.local)
    results = run(engine, clock, 40 * 60, in_use=True)
    assert sum(r.just_blocked for r in results) == 1
    assert [w for r in results for w in r.warnings] == [15, 5]


def test_no_warning_without_browser():
    state = make()
    clock = Clock(at(5, "22:00"))
    engine = Engine(state)
    engine.start(clock.wall, clock.mono, clock.local)
    assert not any(r.warnings for r in run(engine, clock, 30 * 60, in_use=False))


def test_pending_default_change_waits_loosen_delay():
    state = make()
    desired = copy.deepcopy(state.active)
    desired["bedtime"] = "23:30"
    pl_rules.reconcile(state, desired, rules.looser_rules(state.active), rules.LIST_LOOSE_OP)
    assert state.active["bedtime"] == "22:30" and state.pending
    clock = Clock(at(5, "12:00"))
    engine = Engine(state)
    engine.start(clock.wall, clock.mono, clock.local)
    run(engine, clock, 24 * HOUR, in_use=False, step=60)
    assert state.active["bedtime"] == "23:30"


def test_default_changes_tighten_and_loosen():
    state = make()
    desired = copy.deepcopy(state.active)
    desired.update(bedtime="21:30", grace_minutes=10)
    desired["apps"] = [b for b in desired["apps"] if b != "chrome.exe"] + ["iexplore.exe"]
    r = pl_rules.reconcile(state, desired, rules.looser_rules(state.active), rules.LIST_LOOSE_OP)
    assert state.active["bedtime"] == "21:30"  # earlier: immediate
    assert "iexplore.exe" in state.active["apps"]  # adding: immediate
    assert {p["key"] for p in r.new_pending} == {"grace_minutes", "apps"}
    assert "chrome.exe" in state.active["apps"]  # removing: waits


def test_bedtime_after_midnight_is_looser():
    looser = rules.looser_rules(DEFAULTS)["bedtime"]
    assert looser("22:30", "00:30")
    assert not looser("00:30", "22:30")


# ---------------------------------------------------------------- config

def test_config_roundtrip_and_defaults():
    cfg = parse_config(render_config(DEFAULTS))
    assert cfg == {**DEFAULTS, "apps": sorted(DEFAULTS["apps"])}
    assert cfg["wake_time"] == "07:00"
    assert cfg["grace_minutes"] == 5


def test_example_config_parses():
    from curfew.store import EXAMPLE_CONFIG

    assert parse_config(EXAMPLE_CONFIG.read_text(encoding="utf-8")) == parse_config("")


@pytest.mark.parametrize("text", ['bedtime = "25:00"', 'bedtime = "07:00"', "grace_minutes = 61", "nope = 1"])
def test_config_rejects(text):
    with pytest.raises(ConfigError):
        parse_config(text)


def test_parse_time_normalises():
    assert parse_time("t", " 9:05 ") == "09:05"


# ---------------------------------------------------------------- detection

def test_only_main_browser_processes_are_targeted():
    procs = {
        10: ("chrome.exe", 1),  # main
        11: ("chrome.exe", 10),  # helper
        12: ("chrome.exe", 10),
        20: ("firefox.exe", 2),
        30: ("chrome.exe", 99),  # helper whose parent is gone
    }
    assert main_processes(procs) == {10: "chrome.exe", 20: "firefox.exe", 30: "chrome.exe"}


# ---------------------------------------------------------------- apps and games

def test_defaults_include_chat_apps():
    for exe in ("discord.exe", "ms-teams.exe", "teams.exe", "olk.exe", "outlook.exe", "chrome.exe"):
        assert exe in DEFAULTS["apps"]
    assert DEFAULTS["close_games"] is True


def test_old_browsers_key_still_parses():
    cfg = parse_config('browsers = ["chrome.exe"]')
    assert cfg["apps"] == ["chrome.exe"]
    with pytest.raises(ConfigError):
        parse_config('browsers = ["a.exe"]\napps = ["b.exe"]')


def test_old_state_is_migrated():
    old = make().to_dict()
    old["active"]["browsers"] = old["active"].pop("apps")
    del old["active"]["close_games"]
    old["pending"] = [{"key": "browsers", "op": "remove", "item": "x.exe", "remaining_s": 5.0}]
    state = State.from_dict(old)
    assert "browsers" not in state.active and state.active["apps"]
    assert state.active["close_games"] is True
    assert state.pending[0]["key"] == "apps"


def test_close_games_off_is_looser_on_is_immediate():
    state = make(close_games=True)
    desired = copy.deepcopy(state.active)
    desired["close_games"] = False
    r = pl_rules.reconcile(state, desired, rules.looser_rules(state.active), rules.LIST_LOOSE_OP)
    assert state.active["close_games"] is True and [p["key"] for p in r.new_pending] == ["close_games"]

    state = make(close_games=False)
    desired = copy.deepcopy(state.active)
    desired["close_games"] = True
    pl_rules.reconcile(state, desired, rules.looser_rules(state.active), rules.LIST_LOOSE_OP)
    assert state.active["close_games"] is True and not state.pending


def test_close_games_roundtrips_through_config_file():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["close_games"] = False
    assert parse_config(render_config(cfg))["close_games"] is False
    with pytest.raises(ConfigError):
        parse_config("close_games = 1")


def test_scanner_targets_listed_apps_and_games(monkeypatch):
    from curfew import detect

    class FakeGames:
        def is_game(self, exe):
            return exe.lower().startswith("d:\steamlibrary\steamapps\common\\")

    monkeypatch.setattr(detect, "GameScanner", FakeGames)
    s = detect.AppScanner()
    s.configure(["discord.exe", "ms-teams.exe"], close_games=True)
    assert s.is_target(r"C:\Users\x\AppData\Local\Discord\app-1.0\Discord.exe")
    assert s.is_target(r"D:\SteamLibrary\steamapps\common\Game\game.exe")
    assert not s.is_target(r"C:\Windows\notepad.exe")
    s.configure(["discord.exe"], close_games=False)
    assert not s.is_target(r"D:\SteamLibrary\steamapps\common\Game\game.exe")
