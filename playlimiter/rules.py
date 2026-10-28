"""Pure rules logic: sessions, cooldown, daily cap, delayed loosening, delayed uninstall.

Nothing in here touches Windows, files or the real clock. Callers pass in the
times, so the rules can be driven by a fake clock in tests.

Session model
-------------
Launching a game starts a session. The session clock then runs continuously,
whether or not a game is still open, so quitting early doesn't save time for
later. When it reaches ``session_minutes`` the cooldown starts and runs for
``cooldown_minutes``. After that nothing counts until a game is launched again,
which starts a fresh session. The daily cap is different: it counts only time
a game is actually running.

Time model
----------
* ``mono`` is a clock that only moves forward within one boot (the daemon uses
  GetTickCount64, which keeps counting through sleep). All durations
  (budget, cooldown, pending changes, uninstall) are advanced with mono deltas,
  so changing the wall clock has no effect on them.
* ``wall`` (epoch seconds) is only used to bridge a reboot, and a negative
  wall delta counts as zero.
* ``now_local`` (naive local datetime) is only used for the daily reset. The
  stored day never moves backwards, so setting the clock back cannot trigger an
  extra reset.
"""
from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

DEFAULTS: dict = {
    "session_minutes": 90,
    "cooldown_minutes": 180,
    "daily_cap_minutes": 0,
    "warning_minutes": [10, 2],
    "grace_seconds": 60,
    "loosen_delay_hours": 24,
    "daily_reset_time": "04:00",
    "exclude": [],
    "extra_paths": [],
}

# A tick longer than this while a game is running means the machine was asleep
# or the daemon was stalled. Only this much of it counts toward the daily cap.
MAX_PLAY_TICK_S = 30.0


def _cap(minutes: float) -> float:
    return math.inf if minutes == 0 else minutes


# For each scalar setting: is going from old to new LOOSER (more play)?
LOOSER = {
    "session_minutes": lambda old, new: new > old,
    "cooldown_minutes": lambda old, new: new < old,
    "daily_cap_minutes": lambda old, new: _cap(new) > _cap(old),
    "grace_seconds": lambda old, new: new > old,
    "loosen_delay_hours": lambda old, new: new < old,
    # Moving the reset time can grant an extra reset, so any change is looser.
    "daily_reset_time": lambda old, new: new != old,
    # Warnings don't change how long you can play.
    "warning_minutes": lambda old, new: False,
}

# For list settings: which operation is the loosening one.
# Adding an exclusion = fewer things counted = looser.
# Removing an extra path = fewer things counted = looser.
LIST_LOOSE_OP = {"exclude": "add", "extra_paths": "remove"}


@dataclass
class State:
    active: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    pending: list = field(default_factory=list)
    session_elapsed_s: float | None = None  # None = no session running
    cooldown_elapsed_s: float | None = None  # None = not in cooldown
    daily_used_s: float = 0.0
    day_key: int | None = None
    warned: list = field(default_factory=list)
    # Open from the daily reset until the first game of the day: all setting changes apply instantly.
    free_change_window: bool = False
    uninstall_remaining_s: float | None = None
    last_wall: float | None = None
    last_mono: float | None = None
    clock_skew_s: float = 0.0  # net wall-clock changes seen while running (see honest_local)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        state = cls(**known)
        # Fill any settings added in newer versions.
        for k, v in DEFAULTS.items():
            state.active.setdefault(k, copy.deepcopy(v))
        return state


@dataclass
class Status:
    blocked: bool
    reason: str | None  # "cooldown" | "daily_cap" | None
    wait_s: float  # seconds until play is allowed again (0 if not blocked)
    remaining_s: float  # play seconds left before blocking (0 if blocked)


@dataclass
class TickResult:
    status: Status
    just_blocked: bool = False
    warnings: list = field(default_factory=list)  # minutes thresholds just crossed
    applied: list = field(default_factory=list)  # pending changes that took effect
    uninstall_due: bool = False


# ---------------------------------------------------------------- daily reset

def _reset_offset(reset_time: str) -> timedelta:
    h, m = reset_time.split(":")
    return timedelta(hours=int(h), minutes=int(m))


def day_key(now_local: datetime, reset_time: str) -> int:
    return (now_local - _reset_offset(reset_time)).date().toordinal()


def next_reset(state: State) -> datetime:
    return datetime.fromordinal(state.day_key + 1) + _reset_offset(state.active["daily_reset_time"])


def _maybe_daily_reset(state: State, now_local: datetime) -> list:
    """Reset the daily counter at the reset time and open the free-change window.

    Returns the pending changes that were applied because the window opened.
    """
    key = day_key(now_local, state.active["daily_reset_time"])
    if state.day_key is None:
        state.day_key = key
        return []
    if key <= state.day_key:  # never move backwards
        return []
    state.day_key = key
    state.daily_used_s = 0.0
    state.free_change_window = True
    applied = state.pending
    for p in applied:
        _apply_pending(state.active, p)
    state.pending = []
    return applied


# ---------------------------------------------------------------- evaluation

def evaluate(state: State, now_local: datetime) -> Status:
    cfg = state.active
    session_s = cfg["session_minutes"] * 60
    cooldown_s = cfg["cooldown_minutes"] * 60
    cap_s = _cap(cfg["daily_cap_minutes"]) * 60

    waits = []
    reason = None
    if state.cooldown_elapsed_s is not None:
        reason = "cooldown"
        waits.append(max(0.0, cooldown_s - state.cooldown_elapsed_s))
    if state.daily_used_s >= cap_s:
        reason = reason or "daily_cap"
        waits.append(max(0.0, (next_reset(state) - now_local).total_seconds()))
    if reason:
        return Status(True, reason, max(waits), 0.0)
    session_left = session_s - (state.session_elapsed_s or 0.0)
    remaining = min(session_left, cap_s - state.daily_used_s)
    return Status(False, None, 0.0, max(0.0, remaining))


def session_ends_in(state: State) -> float | None:
    """Seconds until the running session ends, or None if no session is running."""
    if state.session_elapsed_s is None:
        return None
    return max(0.0, state.active["session_minutes"] * 60 - state.session_elapsed_s)


def _pass_time(state: State, seconds: float) -> None:
    """Advance the session and cooldown clocks. Both run on time, not on play."""
    cfg = state.active
    if state.session_elapsed_s is not None:
        state.session_elapsed_s += seconds
        over = state.session_elapsed_s - cfg["session_minutes"] * 60
        if over < 0:
            return
        # Session over: the cooldown starts at the moment it ended.
        state.session_elapsed_s = None
        state.cooldown_elapsed_s = 0.0
        seconds = over
    if state.cooldown_elapsed_s is not None:
        state.cooldown_elapsed_s += seconds
        if state.cooldown_elapsed_s >= cfg["cooldown_minutes"] * 60:
            state.cooldown_elapsed_s = None


def _count_down_timers(state: State, elapsed: float) -> tuple[list, bool]:
    applied = []
    still = []
    for p in state.pending:
        p["remaining_s"] -= elapsed
        if p["remaining_s"] <= 0:
            _apply_pending(state.active, p)
            applied.append(p)
        else:
            still.append(p)
    state.pending = still
    due = False
    if state.uninstall_remaining_s is not None:
        state.uninstall_remaining_s = max(0.0, state.uninstall_remaining_s - elapsed)
        due = state.uninstall_remaining_s <= 0
    return applied, due


def advance(state: State, elapsed: float, playing: bool, now_local: datetime) -> TickResult:
    """Move the state forward by ``elapsed`` trusted seconds."""
    elapsed = max(0.0, elapsed)
    blocked_before = evaluate(state, now_local).blocked

    applied = _maybe_daily_reset(state, now_local)
    timed_out, uninstall_due = _count_down_timers(state, elapsed)
    applied += timed_out

    if playing:
        state.free_change_window = False  # the first game of the day closes it
        state.daily_used_s += min(elapsed, MAX_PLAY_TICK_S)
        if state.session_elapsed_s is None and not evaluate(state, now_local).blocked:
            state.session_elapsed_s = 0.0  # a game launch starts a session
    # Also runs with elapsed 0, so a session shortened by a config change ends right away.
    _pass_time(state, elapsed)

    status = evaluate(state, now_local)

    # Warnings: a threshold is re-armed once remaining time is above it again.
    thresholds = state.active["warning_minutes"]
    state.warned = [w for w in state.warned if status.remaining_s <= w * 60]
    warnings = []
    if playing and not status.blocked:
        crossed = [w for w in thresholds if status.remaining_s <= w * 60 and w not in state.warned]
        if crossed:
            warnings = [min(crossed)]
            state.warned.extend(crossed)

    return TickResult(
        status=status,
        just_blocked=status.blocked and not blocked_before,
        warnings=warnings,
        applied=applied,
        uninstall_due=uninstall_due,
    )


# ---------------------------------------------------------------- clock bridging

def downtime_credit(state: State, wall_now: float, mono_now: float) -> float:
    """Seconds of downtime to count when the daemon starts.

    * Same boot (mono went forward): the daemon was killed or the user logged
      off. Games could have run unchecked, so nothing is credited.
    * Rebooted (mono went backwards): credit the wall-clock gap as time passed
      (it advances a running session or cooldown), but never a negative one.
    """
    if state.last_wall is None or state.last_mono is None:
        return 0.0
    if mono_now >= state.last_mono:
        return 0.0
    return max(0.0, wall_now - state.last_wall)


def honest_local(state: State, now_local: datetime) -> datetime:
    """Local time with any forward clock change taken back out.

    Used for the daily reset, so moving the clock forward can't trigger an
    early reset (or open the free-change window). Backward changes need no
    correction: the stored day never moves backwards anyway.
    """
    return now_local - timedelta(seconds=max(0.0, state.clock_skew_s))


class Engine:
    """Wraps a State and turns (wall, mono) clock readings into trusted elapsed time."""

    def __init__(self, state: State):
        self.state = state

    def start(self, wall: float, mono: float, now_local: datetime) -> TickResult:
        credit = downtime_credit(self.state, wall, mono)
        self._stamp(wall, mono)
        return advance(self.state, credit, playing=False, now_local=honest_local(self.state, now_local))

    def tick(self, wall: float, mono: float, now_local: datetime, playing: bool) -> TickResult:
        s = self.state
        if s.last_mono is None or s.last_wall is None:
            elapsed = 0.0
        else:
            elapsed = max(0.0, mono - s.last_mono)
            # Wall-clock movement that uptime doesn't explain = the clock was changed.
            s.clock_skew_s += (wall - s.last_wall) - (mono - s.last_mono)
        self._stamp(wall, mono)
        return advance(s, elapsed, playing, honest_local(s, now_local))

    def _stamp(self, wall: float, mono: float) -> None:
        self.state.last_wall = wall
        self.state.last_mono = mono


# ---------------------------------------------------------------- loosening

def _apply_pending(active: dict, p: dict) -> None:
    if "op" in p:
        items = set(active[p["key"]])
        if p["op"] == "add":
            items.add(p["item"])
        else:
            items.discard(p["item"])
        active[p["key"]] = sorted(items)
    else:
        active[p["key"]] = copy.deepcopy(p["value"])


def describe_change(p: dict) -> str:
    if "op" in p:
        verb = "add to" if p["op"] == "add" else "remove from"
        return f"{verb} {p['key']}: {p['item']}"
    return f"{p['key']} -> {p['value']}"


@dataclass
class ReconcileResult:
    immediate: list = field(default_factory=list)  # descriptions applied immediately
    new_pending: list = field(default_factory=list)  # pending entries created or restarted

    @property
    def tightened(self) -> list:
        # Old name, still used by Browser Curfew (curfew/daemon.py, curfew/gui.py).
        return self.immediate


def reconcile(state: State, desired: dict, looser: dict = LOOSER, list_loose_op: dict = LIST_LOOSE_OP) -> ReconcileResult:
    """Compare the edited config with the active one.

    Stricter changes apply immediately. Looser changes become pending for
    ``loosen_delay_hours`` (the *active* value, so lowering the delay can't
    speed itself up). Pending changes that are no longer wanted are dropped.
    ``looser``/``list_loose_op`` let another app (Browser Curfew) reuse this with its own settings.

    While the free-change window is open (after the daily reset, until the
    first game of the day), every change applies immediately.
    """
    active = state.active
    delay_s = active["loosen_delay_hours"] * 3600
    result = ReconcileResult()

    # getattr: Browser Curfew passes its own State, which has no free-change window.
    if getattr(state, "free_change_window", False):
        result.immediate = [f"{k} -> {desired[k]}" for k in desired if desired[k] != active.get(k)]
        state.active = copy.deepcopy(desired)
        state.pending = []
        return result

    for key, is_looser in looser.items():
        old, new = active[key], desired[key]
        existing = next((p for p in state.pending if p["key"] == key and "op" not in p), None)
        if existing:
            state.pending.remove(existing)
        if new == old:
            continue
        if not is_looser(old, new):
            active[key] = copy.deepcopy(new)
            result.immediate.append(f"{key} -> {new}")
            continue
        if existing and (existing["value"] == new or not is_looser(existing["value"], new)):
            # Same or less loose than what was already waiting: keep its timer.
            entry = {"key": key, "value": copy.deepcopy(new), "remaining_s": existing["remaining_s"]}
        else:
            entry = {"key": key, "value": copy.deepcopy(new), "remaining_s": delay_s}
            result.new_pending.append(entry)
        state.pending.append(entry)

    for key, loose_op in list_loose_op.items():
        old, new = set(active[key]), set(desired[key])
        added, removed = new - old, old - new
        strict_items = removed if loose_op == "add" else added
        loose_items = added if loose_op == "add" else removed
        strict_op = "remove" if loose_op == "add" else "add"

        for item in sorted(strict_items):
            _apply_pending(active, {"key": key, "op": strict_op, "item": item})
            result.immediate.append(describe_change({"key": key, "op": strict_op, "item": item}))

        kept = []
        for p in state.pending:
            if p["key"] != key:
                kept.append(p)
            elif p["item"] in loose_items:
                kept.append(p)
                loose_items.discard(p["item"])
        state.pending = kept
        for item in sorted(loose_items):
            entry = {"key": key, "op": loose_op, "item": item, "remaining_s": delay_s}
            state.pending.append(entry)
            result.new_pending.append(entry)

    return result


# ---------------------------------------------------------------- uninstall

def request_uninstall(state: State) -> float:
    """Schedule removal. Asking again does not restart an existing timer."""
    if state.uninstall_remaining_s is None:
        state.uninstall_remaining_s = state.active["loosen_delay_hours"] * 3600
    return state.uninstall_remaining_s


def start_cooldown(state: State) -> bool:
    """Start the cooldown now, even with play time left. It's stricter, so no delay."""
    if state.cooldown_elapsed_s is not None:
        return False
    state.session_elapsed_s = None
    state.cooldown_elapsed_s = 0.0
    return True


def cancel_uninstall(state: State) -> bool:
    was = state.uninstall_remaining_s is not None
    state.uninstall_remaining_s = None
    return was
