"""Pure rules logic for Browser Curfew: nightly block window, tonight's one-off change, settings.

Nothing in here touches Windows, files or the real clock, so tests can drive it with a fake one.

The curfew day
--------------
A "day" runs from one ``wake_time`` to the next (07:00 to 07:00 by default), so 01:00
belongs to the evening before it. The apps are blocked from that day's bedtime until the
next wake_time. Times are compared by how far they are past wake_time, so "00:30" is later
than "22:30".

Tonight's bedtime can be changed, separately from the nightly default in config.toml:

* Earlier is stricter, so it is allowed at any time.
* Later is allowed once per day, before ``change_deadline``, and not once the curfew has started.

Either way the change only covers that day; the next day is back to the default.

Default settings in config.toml use PlayLimiter's rules: stricter changes apply right away,
and looser ones wait ``loosen_delay_hours``.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from playlimiter.rules import _count_down_timers, day_key, downtime_credit

DEFAULTS: dict = {
    "bedtime": "22:30",
    "wake_time": "07:00",
    "change_deadline": "16:00",
    "warning_minutes": [15],
    "grace_minutes": 5,
    "loosen_delay_hours": 24,
    # Also close games (the same Steam-library detection PlayLimiter uses).
    "close_games": True,
    "apps": [
        # Browsers
        "arc.exe",
        "brave.exe",
        "chrome.exe",
        "chromium.exe",
        "firefox.exe",
        "librewolf.exe",
        "msedge.exe",
        "opera.exe",
        "vivaldi.exe",
        "waterfox.exe",
        "zen.exe",
        # Chat and work
        "discord.exe",
        "ms-teams.exe",  # new Teams
        "teams.exe",  # classic Teams
        "olk.exe",  # new Outlook
        "outlook.exe",  # classic Outlook
    ],
}


class CurfewError(ValueError):
    """A change to tonight's curfew that the rules don't allow. The message says why."""


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def since_wake(hhmm: str, wake_time: str) -> int:
    """Minutes from wake_time to hhmm, so later in the evening (or after midnight) is bigger."""
    return (minutes(hhmm) - minutes(wake_time)) % (24 * 60)


def looser_rules(active: dict) -> dict:
    """For each scalar setting: is going from old to new LOOSER (more time on the apps)?"""
    wake = active["wake_time"]
    return {
        "bedtime": lambda old, new: since_wake(new, wake) > since_wake(old, wake),
        # Moving the wake time shifts the whole night, so any change is looser.
        "wake_time": lambda old, new: new != old,
        "change_deadline": lambda old, new: since_wake(new, wake) > since_wake(old, wake),
        "grace_minutes": lambda old, new: new > old,
        "loosen_delay_hours": lambda old, new: new < old,
        # Warnings don't change when apps close.
        "warning_minutes": lambda old, new: False,
        "close_games": lambda old, new: old and not new,
    }


# Removing an app = fewer things closed = looser.
LIST_LOOSE_OP = {"apps": "remove"}


@dataclass
class State:
    active: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    pending: list = field(default_factory=list)
    tonight: dict | None = None  # {"day": curfew day, "time": "HH:MM"}: that day only
    change_used_day: int | None = None  # curfew day whose one later-change was used
    was_blocked: bool = False
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
        # "browsers" was renamed to "apps" when chat apps and games were added.
        if "browsers" in state.active:
            state.active["apps"] = state.active.pop("browsers")
        for p in state.pending:
            if p.get("key") == "browsers":
                p["key"] = "apps"
        for k, v in DEFAULTS.items():
            state.active.setdefault(k, copy.deepcopy(v))
        return state


@dataclass
class Status:
    blocked: bool
    day: int
    bedtime: str  # tonight's effective time
    changed: bool  # tonight differs from the nightly default
    starts_at: datetime
    ends_at: datetime
    deadline: datetime  # last moment tonight can be made later
    change_used: bool


@dataclass
class TickResult:
    status: Status
    just_blocked: bool = False
    warnings: list = field(default_factory=list)  # minutes thresholds just crossed
    applied: list = field(default_factory=list)  # pending changes that took effect
    uninstall_due: bool = False


# ---------------------------------------------------------------- evaluation

def _at(day: int, hhmm: str, wake_time: str) -> datetime:
    """The moment hhmm happens during curfew day ``day``."""
    start = datetime.fromordinal(day) + timedelta(minutes=minutes(wake_time))
    return start + timedelta(minutes=since_wake(hhmm, wake_time))


def evaluate(state: State, now_local: datetime) -> Status:
    cfg = state.active
    wake = cfg["wake_time"]
    day = day_key(now_local, wake)
    tonight = state.tonight["time"] if state.tonight and state.tonight["day"] == day else None
    bedtime = tonight or cfg["bedtime"]
    starts = _at(day, bedtime, wake)
    return Status(
        blocked=now_local >= starts,
        day=day,
        bedtime=bedtime,
        changed=bedtime != cfg["bedtime"],
        starts_at=starts,
        ends_at=_at(day + 1, wake, wake),
        deadline=_at(day, cfg["change_deadline"], wake),
        change_used=state.change_used_day == day,
    )


def later_refusal(state: State, now_local: datetime) -> str | None:
    """Why tonight can't be made later right now, or None if it can."""
    st = evaluate(state, now_local)
    if st.blocked:
        return f"The curfew has already started. Apps are allowed again at {state.active['wake_time']}."
    if st.change_used:
        return "You've already used today's change. Tonight can still be made earlier."
    if now_local >= st.deadline:
        return f"It's past {state.active['change_deadline']}, so tonight can't be made later. It can still be made earlier."
    return None


def set_tonight(state: State, now_local: datetime, time: str) -> str:
    """Change tonight's bedtime. Returns a message; raises CurfewError if not allowed."""
    st = evaluate(state, now_local)
    wake = state.active["wake_time"]
    default = state.active["bedtime"]
    new, current = since_wake(time, wake), since_wake(st.bedtime, wake)
    if new == 0:
        raise CurfewError(f"Tonight's curfew can't be the same as the wake time ({wake}).")
    if new == current:
        return f"Tonight's curfew is already {time}."
    if new > current:
        reason = later_refusal(state, now_local)
        if reason:
            raise CurfewError(reason)
        state.change_used_day = st.day
    state.tonight = {"day": st.day, "time": time}
    if new > current:
        return f"Tonight's curfew is now {time}. That was today's change. Tomorrow it's back to {default}."
    if now_local >= _at(st.day, time, wake):
        return f"Tonight's curfew is now {time}, so it starts now."
    return f"Tonight's curfew is now {time}." + ("" if time == default else f" Tomorrow it's back to {default}.")


# ---------------------------------------------------------------- ticking

def advance(state: State, elapsed: float, in_use: bool, now_local: datetime) -> TickResult:
    """Move the state forward by ``elapsed`` trusted seconds (only pending timers use it)."""
    applied, uninstall_due = _count_down_timers(state, max(0.0, elapsed))
    st = evaluate(state, now_local)
    just_blocked = st.blocked and not state.was_blocked
    state.was_blocked = st.blocked

    # Warnings: a threshold is re-armed once bedtime is further away than it again
    # (tonight moved later, or a new day).
    left_s = (st.starts_at - now_local).total_seconds()
    state.warned = [w for w in state.warned if left_s <= w * 60]
    warnings = []
    if in_use and not st.blocked:
        crossed = [w for w in state.active["warning_minutes"] if left_s <= w * 60 and w not in state.warned]
        if crossed:
            warnings = [min(crossed)]
            state.warned.extend(crossed)

    return TickResult(st, just_blocked, warnings, applied, uninstall_due)


class Engine:
    """Wraps a State and turns (wall, mono) clock readings into trusted elapsed time."""

    def __init__(self, state: State):
        self.state = state

    def start(self, wall: float, mono: float, now_local: datetime) -> TickResult:
        credit = downtime_credit(self.state, wall, mono)
        self._stamp(wall, mono)
        return advance(self.state, credit, in_use=False, now_local=now_local)

    def tick(self, wall: float, mono: float, now_local: datetime, in_use: bool) -> TickResult:
        last = self.state.last_mono
        elapsed = 0.0 if last is None else max(0.0, mono - last)
        self._stamp(wall, mono)
        return advance(self.state, elapsed, in_use, now_local)

    def _stamp(self, wall: float, mono: float) -> None:
        self.state.last_wall = wall
        self.state.last_mono = mono
