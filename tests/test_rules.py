import copy
from datetime import datetime, timedelta

import pytest

from playlimiter import rules
from playlimiter.config import ConfigError, parse_config, render_config
from playlimiter.rules import DEFAULTS, Engine, State
from playlimiter.steam import library_paths_from_vdf

MIN = 60
HOUR = 3600


class FakeClock:
    """wall/mono/local all start together; jumps can desynchronise them."""

    def __init__(self, start=datetime(2026, 1, 5, 12, 0)):
        self.local = start
        self.wall = 1_000_000.0
        self.mono = 500.0

    def advance(self, seconds):
        self.local += timedelta(seconds=seconds)
        self.wall += seconds
        self.mono += seconds

    def reboot(self, downtime):
        self.advance(downtime)
        self.mono = 10.0  # uptime restarts after a reboot


def make(**overrides):
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(overrides)
    state = State(active=cfg)
    clock = FakeClock()
    engine = Engine(state)
    engine.start(clock.wall, clock.mono, clock.local)
    return state, engine, clock


def run(engine, clock, seconds, playing, step=2):
    """Simulate the daemon polling every `step` seconds. Returns all results."""
    out = []
    for _ in range(int(seconds // step)):
        clock.advance(step)
        out.append(engine.tick(clock.wall, clock.mono, clock.local, playing))
    return out


def status(engine, clock):
    return rules.evaluate(engine.state, clock.local)


# ---------------------------------------------------------------- sessions

def test_budget_exhausts_and_blocks():
    state, engine, clock = make()
    results = run(engine, clock, 90 * MIN, playing=True)
    assert results[-1].status.blocked
    assert results[-1].status.reason == "cooldown"
    assert sum(r.just_blocked for r in results) == 1


def test_nothing_counts_before_a_game_is_launched():
    state, engine, clock = make()
    run(engine, clock, 5 * HOUR, playing=False, step=60)
    assert state.session_elapsed_s is None
    assert status(engine, clock).remaining_s == 90 * MIN


def test_session_keeps_running_after_quitting_early():
    state, engine, clock = make()
    run(engine, clock, 60 * MIN, playing=True)
    run(engine, clock, 29 * MIN, playing=False)  # quit with 30 min left
    assert status(engine, clock).remaining_s == pytest.approx(1 * MIN)
    results = run(engine, clock, 1 * MIN, playing=False)
    assert results[-1].status.blocked and results[-1].status.reason == "cooldown"
    assert sum(r.just_blocked for r in results) == 1
    # The break started when the session ended (minute 90), not when the game closed.
    assert status(engine, clock).wait_s == pytest.approx(180 * MIN)


def test_early_quit_and_relaunch_does_not_reset_session():
    state, engine, clock = make()
    run(engine, clock, 89 * MIN, playing=True)
    run(engine, clock, 30, playing=False)
    results = run(engine, clock, 30, playing=True)
    assert results[-1].status.blocked


def test_new_full_session_after_cooldown_starts_on_next_launch():
    state, engine, clock = make()
    run(engine, clock, 90 * MIN, playing=True)
    run(engine, clock, 179 * MIN, playing=False)
    assert status(engine, clock).blocked
    run(engine, clock, 1 * MIN, playing=False)
    s = status(engine, clock)
    assert not s.blocked and s.remaining_s == 90 * MIN
    assert state.session_elapsed_s is None
    run(engine, clock, 2 * HOUR, playing=False, step=60)  # waiting doesn't start anything
    assert state.session_elapsed_s is None
    run(engine, clock, 10 * MIN, playing=True)
    assert status(engine, clock).remaining_s == pytest.approx(80 * MIN)


def test_launch_during_cooldown_stays_blocked_and_reports_wait():
    state, engine, clock = make()
    run(engine, clock, 90 * MIN, playing=True)
    run(engine, clock, 60 * MIN, playing=False)
    r = run(engine, clock, 30, playing=True)[-1]
    assert r.status.blocked and not r.just_blocked
    assert r.status.wait_s == pytest.approx(120 * MIN - 30)


def test_warnings_fire_once_each():
    state, engine, clock = make()
    results = run(engine, clock, 90 * MIN, playing=True)
    warned = [w for r in results for w in r.warnings]
    assert warned == [10, 2]


def test_relaunch_below_threshold_warns_once():
    state, engine, clock = make()
    run(engine, clock, 85 * MIN, playing=True)  # 10-min warning fires here
    run(engine, clock, 2 * MIN, playing=False)
    results = run(engine, clock, 2 * MIN, playing=True)
    assert [w for r in results for w in r.warnings] == [2]


def test_no_warning_while_not_playing():
    state, engine, clock = make()
    run(engine, clock, 10 * MIN, playing=True)
    results = run(engine, clock, 80 * MIN, playing=False)
    assert not any(r.warnings for r in results)


def test_sleep_with_game_open_counts_for_session_not_daily_cap():
    state, engine, clock = make()
    run(engine, clock, 10 * MIN, playing=True)
    clock.advance(8 * HOUR)  # laptop slept with game open
    engine.tick(clock.wall, clock.mono, clock.local, True)
    # Session (90) and cooldown (180) both ran out during the sleep.
    assert state.cooldown_elapsed_s is None
    assert state.daily_used_s <= 10 * MIN + rules.MAX_PLAY_TICK_S


# ---------------------------------------------------------------- daily cap

def test_daily_cap_blocks_until_reset_time():
    state, engine, clock = make(daily_cap_minutes=60, session_minutes=120)
    r = run(engine, clock, 60 * MIN, playing=True)[-1]  # 12:00 -> 13:00
    assert r.status.blocked and r.status.reason == "daily_cap"
    # next reset is 04:00 tomorrow
    assert r.status.wait_s == pytest.approx(15 * HOUR)
    run(engine, clock, 15 * HOUR, playing=False, step=60)
    assert not status(engine, clock).blocked
    assert state.daily_used_s == 0


def test_daily_cap_zero_means_off():
    state, engine, clock = make(daily_cap_minutes=0, session_minutes=600)
    r = run(engine, clock, 5 * HOUR, playing=True, step=10)[-1]
    assert not r.status.blocked


# ---------------------------------------------------------------- persistence / clock

def roundtrip(state):
    return State.from_dict(copy.deepcopy(state.to_dict()))


def test_reboot_during_cooldown_keeps_cooldown_and_credits_downtime():
    state, engine, clock = make()
    run(engine, clock, 90 * MIN, playing=True)
    run(engine, clock, 30 * MIN, playing=False)
    saved = roundtrip(state)

    clock.reboot(60 * MIN)
    engine2 = Engine(saved)
    engine2.start(clock.wall, clock.mono, clock.local)
    s = rules.evaluate(saved, clock.local)
    assert s.blocked and s.reason == "cooldown"
    assert s.wait_s == pytest.approx(90 * MIN)


def test_reboot_mid_session_keeps_session_and_counts_the_reboot():
    state, engine, clock = make()
    run(engine, clock, 45 * MIN, playing=True)
    saved = roundtrip(state)
    clock.reboot(2 * MIN)
    engine2 = Engine(saved)
    engine2.start(clock.wall, clock.mono, clock.local)
    # The session clock keeps running through the reboot.
    assert rules.evaluate(saved, clock.local).remaining_s == pytest.approx(43 * MIN)


def test_same_boot_restart_credits_no_downtime():
    state, engine, clock = make()
    run(engine, clock, 90 * MIN, playing=True)
    saved = roundtrip(state)
    clock.advance(3 * HOUR)  # daemon was killed, same boot
    Engine(saved).start(clock.wall, clock.mono, clock.local)
    assert rules.evaluate(saved, clock.local).reason == "cooldown"
    assert saved.cooldown_elapsed_s == pytest.approx(0)


def test_backwards_clock_jump_does_not_shorten_cooldown_or_refill():
    state, engine, clock = make()
    run(engine, clock, 90 * MIN, playing=True)
    run(engine, clock, 10 * MIN, playing=False)
    before = state.cooldown_elapsed_s
    # Wall clock set back 2 days; mono keeps going
    clock.wall -= 2 * 24 * HOUR
    clock.local -= timedelta(days=2)
    run(engine, clock, 10, playing=False)
    assert state.cooldown_elapsed_s == pytest.approx(before + 10)


def test_backwards_jump_across_reboot_credits_nothing():
    state, engine, clock = make()
    run(engine, clock, 90 * MIN, playing=True)
    saved = roundtrip(state)
    clock.reboot(0)
    clock.wall -= 5 * HOUR
    clock.local -= timedelta(hours=5)
    Engine(saved).start(clock.wall, clock.mono, clock.local)
    assert saved.cooldown_elapsed_s == pytest.approx(0)


def test_forward_clock_jump_in_process_does_not_advance_timers():
    state, engine, clock = make()
    run(engine, clock, 90 * MIN, playing=True)
    clock.wall += 10 * HOUR
    run(engine, clock, 10, playing=False)
    assert state.cooldown_elapsed_s == pytest.approx(10)


def test_backwards_jump_does_not_trigger_extra_daily_reset():
    state, engine, clock = make(daily_cap_minutes=60, session_minutes=120)
    run(engine, clock, 60 * MIN, playing=True)
    run(engine, clock, 16 * HOUR, playing=False, step=60)  # past 04:00, reset happened
    run(engine, clock, 60 * MIN, playing=True)
    assert status(engine, clock).reason == "daily_cap"
    clock.local -= timedelta(days=1)
    run(engine, clock, 10, playing=False)
    assert status(engine, clock).reason == "daily_cap"  # still blocked
    clock.local += timedelta(days=1)
    run(engine, clock, 10, playing=False)
    assert status(engine, clock).reason == "daily_cap"


# ---------------------------------------------------------------- loosening

def desired(state, **changes):
    d = copy.deepcopy(state.active)
    d.update(changes)
    return d


@pytest.mark.parametrize(
    "key,value",
    [("session_minutes", 60), ("cooldown_minutes", 240), ("daily_cap_minutes", 120),
     ("grace_seconds", 30), ("loosen_delay_hours", 48), ("warning_minutes", [5])],
)
def test_stricter_changes_apply_immediately(key, value):
    state, engine, clock = make()
    r = rules.reconcile(state, desired(state, **{key: value}))
    assert state.active[key] == value
    assert r.tightened and not r.new_pending and not state.pending


@pytest.mark.parametrize(
    "key,value",
    [("session_minutes", 120), ("cooldown_minutes", 60), ("grace_seconds", 120),
     ("loosen_delay_hours", 1), ("daily_reset_time", "06:00")],
)
def test_looser_changes_are_delayed(key, value):
    state, engine, clock = make()
    old = state.active[key]
    rules.reconcile(state, desired(state, **{key: value}))
    assert state.active[key] == old
    assert len(state.pending) == 1
    run(engine, clock, 24 * HOUR - 60, playing=False, step=60)
    assert state.active[key] == old
    results = run(engine, clock, 120, playing=False, step=60)
    assert state.active[key] == value
    assert any(r.applied for r in results)


def test_turning_off_daily_cap_is_looser():
    state, engine, clock = make(daily_cap_minutes=120)
    rules.reconcile(state, desired(state, daily_cap_minutes=0))
    assert state.active["daily_cap_minutes"] == 120
    assert state.pending[0]["value"] == 0


def test_shorter_session_triggers_cooldown_immediately():
    state, engine, clock = make()
    run(engine, clock, 70 * MIN, playing=True)
    rules.reconcile(state, desired(state, session_minutes=60))
    r = run(engine, clock, 2, playing=True)[-1]
    assert r.status.blocked and r.just_blocked


def test_exclude_add_is_delayed_remove_is_immediate():
    state, engine, clock = make(exclude=["a.exe", "b.exe"])
    rules.reconcile(state, desired(state, exclude=["b.exe", "c.exe"]))
    assert state.active["exclude"] == ["b.exe"]  # a.exe removed now
    assert state.pending == [{"key": "exclude", "op": "add", "item": "c.exe", "remaining_s": 24 * HOUR}]
    run(engine, clock, 24 * HOUR, playing=False, step=60)
    assert state.active["exclude"] == ["b.exe", "c.exe"]


def test_extra_paths_add_is_immediate_remove_is_delayed():
    state, engine, clock = make(extra_paths=["d:\\games"])
    rules.reconcile(state, desired(state, extra_paths=["e:\\games"]))
    assert state.active["extra_paths"] == ["d:\\games", "e:\\games"]
    assert state.pending[0]["op"] == "remove"


def test_reverting_a_pending_change_cancels_it():
    state, engine, clock = make()
    rules.reconcile(state, desired(state, session_minutes=120))
    rules.reconcile(state, desired(state, session_minutes=90))
    assert state.pending == []


def test_lowering_delay_cannot_speed_up_itself_or_others():
    state, engine, clock = make()
    rules.reconcile(state, desired(state, session_minutes=120, loosen_delay_hours=0))
    assert {p["remaining_s"] for p in state.pending} == {24 * HOUR}


def test_moderating_a_pending_change_keeps_its_timer():
    state, engine, clock = make()
    rules.reconcile(state, desired(state, session_minutes=150))
    run(engine, clock, 20 * HOUR, playing=False, step=60)
    rules.reconcile(state, desired(state, session_minutes=120))  # less loose than pending
    assert state.pending[0]["remaining_s"] == pytest.approx(4 * HOUR)
    rules.reconcile(state, desired(state, session_minutes=200))  # looser again: restart
    assert state.pending[0]["remaining_s"] == 24 * HOUR


def test_pending_survives_restart_and_ignores_clock_jump():
    state, engine, clock = make()
    rules.reconcile(state, desired(state, session_minutes=120))
    run(engine, clock, HOUR, playing=False, step=60)
    clock.wall += 48 * HOUR  # clock set forward
    clock.local += timedelta(hours=48)
    run(engine, clock, 60, playing=False)
    assert state.active["session_minutes"] == 90
    saved = roundtrip(state)
    assert saved.pending[0]["remaining_s"] == pytest.approx(23 * HOUR - 60)


def test_start_cooldown_early_blocks_now_and_refills_after():
    state, engine, clock = make()
    run(engine, clock, 20 * MIN, playing=True)
    assert rules.start_cooldown(state)
    assert not rules.start_cooldown(state)  # already running: no restart
    s = status(engine, clock)
    assert s.blocked and s.reason == "cooldown" and s.wait_s == 180 * MIN
    run(engine, clock, 180 * MIN, playing=False, step=60)
    s = status(engine, clock)
    assert not s.blocked and s.remaining_s == 90 * MIN


# ---------------------------------------------------------------- uninstall

def test_delayed_uninstall():
    state, engine, clock = make()
    assert rules.request_uninstall(state) == 24 * HOUR
    results = run(engine, clock, 24 * HOUR - 60, playing=False, step=60)
    assert not any(r.uninstall_due for r in results)
    results = run(engine, clock, 60, playing=False, step=60)
    assert results[-1].uninstall_due


def test_repeat_uninstall_does_not_restart_and_cancel_works():
    state, engine, clock = make()
    rules.request_uninstall(state)
    run(engine, clock, 10 * HOUR, playing=False, step=60)
    assert rules.request_uninstall(state) == pytest.approx(14 * HOUR)
    assert rules.cancel_uninstall(state)
    results = run(engine, clock, 20 * HOUR, playing=False, step=60)
    assert not any(r.uninstall_due for r in results)
    assert not rules.cancel_uninstall(state)


def test_uninstall_survives_reboot():
    state, engine, clock = make()
    rules.request_uninstall(state)
    run(engine, clock, 12 * HOUR, playing=False, step=60)
    saved = roundtrip(state)
    clock.reboot(13 * HOUR)
    assert Engine(saved).start(clock.wall, clock.mono, clock.local).uninstall_due


# ---------------------------------------------------------------- config & vdf

def test_parse_config_defaults_and_normalisation():
    cfg = parse_config('exclude = ["Wallpaper64.EXE", "D:/Tools/X"]\nwarning_minutes = [2, 10, 2]')
    assert cfg["session_minutes"] == 90
    assert cfg["exclude"] == ["d:\\tools\\x", "wallpaper64.exe"]
    assert cfg["warning_minutes"] == [10, 2]


@pytest.mark.parametrize(
    "text",
    ["session_minutes = 0", "session_minutes = 'x'", "daily_reset_time = '25:00'",
     "bogus = 1", "session_minutes = [", "exclude = 'a.exe'", "grace_seconds = true"],
)
def test_parse_config_rejects_invalid(text):
    with pytest.raises(ConfigError):
        parse_config(text)


def test_example_config_matches_defaults():
    from playlimiter.store import EXAMPLE_CONFIG

    assert parse_config(EXAMPLE_CONFIG.read_text(encoding="utf-8")) == DEFAULTS


def test_libraryfolders_vdf():
    text = r'''
"libraryfolders"
{
    "0" { "path" "C:\\Program Files (x86)\\Steam" "apps" { "228980" "1" } }
    "1" { "path" "D:\\SteamLibrary" }
}'''
    assert library_paths_from_vdf(text) == ["C:\\Program Files (x86)\\Steam", "D:\\SteamLibrary"]


def test_render_config_roundtrips():
    assert parse_config(render_config(DEFAULTS)) == DEFAULTS
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(exclude=['d:\\tools\\x "y"', "wallpaper64.exe"], extra_paths=["e:\\spiele\\ü"],
               loosen_delay_hours=1.5, warning_minutes=[15, 5, 1])
    assert parse_config(render_config(cfg)) == cfg
