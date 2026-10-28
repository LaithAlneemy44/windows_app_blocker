"""Pure rules logic: session budget, cooldown, daily cap, delayed loosening, delayed uninstall.

Nothing in here touches Windows, files or the real clock. Callers pass in the
times, so the rules can be driven by a fake clock in tests.

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
# or the daemon was stalled. Only this much of it counts as play, and the rest
# counts as no-play time.
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
    used_s: float = 0.0
    idle_s: float = 0.0
    cooldown_elapsed_s: float | None = None
    daily_used_s: float = 0.0
    day_key: int | None = None
    warned: list = field(default_factory=list)
    uninstall_remaining_s: float | None = None
    last_wall: float | None = None
    last_mono: float | None = None

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


def _maybe_daily_reset(state: State, now_local: datetime) -> None:
    key = day_key(now_local, state.active["daily_reset_time"])
    if state.day_key is None:
        state.day_key = key
    elif key > state.day_key:  # never move backwards
        state.day_key = key
        state.daily_used_s = 0.0


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
    remaining = min(session_s - state.used_s, cap_s - state.daily_used_s)
    return Status(False, None, 0.0, max(0.0, remaining))


def _account(state: State, seconds: float, playing: bool) -> None:
    if seconds <= 0:
        return
    cfg = state.active
    if playing:
        state.daily_used_s += seconds
    if state.cooldown_elapsed_s is not None:
        # Cooldown is a fixed length of time from when the budget ran out.
        state.cooldown_elapsed_s += seconds
        if state.cooldown_elapsed_s >= cfg["cooldown_minutes"] * 60:
            state.cooldown_elapsed_s = None
            state.used_s = 0.0
            state.idle_s = 0.0
        return
    if playing:
        state.used_s += seconds
        state.idle_s = 0.0
    else:
        state.idle_s += seconds
        if state.idle_s >= cfg["cooldown_minutes"] * 60:
            state.used_s = 0.0


def _check_exhausted(state: State) -> None:
    if state.cooldown_elapsed_s is None and state.used_s >= state.active["session_minutes"] * 60:
        state.cooldown_elapsed_s = 0.0
        state.idle_s = 0.0


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

    _maybe_daily_reset(state, now_local)
    applied, uninstall_due = _count_down_timers(state, elapsed)

    play_part = min(elapsed, MAX_PLAY_TICK_S) if playing else 0.0
    _account(state, elapsed - play_part, playing=False)
    _account(state, play_part, playing=True)
    _check_exhausted(state)

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
    * Rebooted (mono went backwards): credit the wall-clock gap as no-play
      time, but never a negative one.
    """
    if state.last_wall is None or state.last_mono is None:
        return 0.0
    if mono_now >= state.last_mono:
        return 0.0
    return max(0.0, wall_now - state.last_wall)


class Engine:
    """Wraps a State and turns (wall, mono) clock readings into trusted elapsed time."""

    def __init__(self, state: State):
        self.state = state

    def start(self, wall: float, mono: float, now_local: datetime) -> TickResult:
        credit = downtime_credit(self.state, wall, mono)
        self._stamp(wall, mono)
        return advance(self.state, credit, playing=False, now_local=now_local)

    def tick(self, wall: float, mono: float, now_local: datetime, playing: bool) -> TickResult:
        last = self.state.last_mono
        elapsed = 0.0 if last is None else max(0.0, mono - last)
        self._stamp(wall, mono)
        return advance(self.state, elapsed, playing, now_local)

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
    tightened: list = field(default_factory=list)  # descriptions applied immediately
    new_pending: list = field(default_factory=list)  # pending entries created or restarted


def reconcile(state: State, desired: dict, looser: dict = LOOSER, list_loose_op: dict = LIST_LOOSE_OP) -> ReconcileResult:
    """Compare the edited config with the active one.

    Stricter changes apply immediately. Looser changes become pending for
    ``loosen_delay_hours`` (the *active* value, so lowering the delay can't
    speed itself up). Pending changes that are no longer wanted are dropped.
    ``looser``/``list_loose_op`` let another app (Browser Curfew) reuse this with its own settings.
    """
    active = state.active
    delay_s = active["loosen_delay_hours"] * 3600
    result = ReconcileResult()

    for key, is_looser in looser.items():
        old, new = active[key], desired[key]
        existing = next((p for p in state.pending if p["key"] == key and "op" not in p), None)
        if existing:
            state.pending.remove(existing)
        if new == old:
            continue
        if not is_looser(old, new):
            active[key] = copy.deepcopy(new)
            result.tightened.append(f"{key} -> {new}")
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
            result.tightened.append(describe_change({"key": key, "op": strict_op, "item": item}))

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
    state.cooldown_elapsed_s = 0.0
    state.idle_s = 0.0
    return True


def cancel_uninstall(state: State) -> bool:
    was = state.uninstall_remaining_s is not None
    state.uninstall_remaining_s = None
    return was
