"""Parse and validate config.toml into a plain dict (pure, no file I/O)."""
from __future__ import annotations

import copy
import json
import ntpath
import re
import tomllib

from .rules import DEFAULTS


class ConfigError(ValueError):
    pass


def _int(name, value, lo, hi=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be a whole number, got {value!r}")
    if value < lo or (hi is not None and value > hi):
        rng = f">= {lo}" if hi is None else f"between {lo} and {hi}"
        raise ConfigError(f"{name} must be {rng}, got {value}")
    return value


def _number(name, value, lo):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    if value < lo:
        raise ConfigError(f"{name} must be >= {lo}, got {value}")
    return value


def _str_list(name, value):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{name} must be a list of strings")
    return value


def normalize_entry(entry: str) -> str:
    """Lower-case, and normalise slashes for anything that looks like a path."""
    entry = entry.strip()
    if "\\" in entry or "/" in entry:
        entry = ntpath.normpath(entry)
    return entry.casefold()


def parse_config(text: str) -> dict:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML: {e}") from e

    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(sorted(unknown))}")

    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(raw)

    _int("session_minutes", cfg["session_minutes"], 1)
    _int("cooldown_minutes", cfg["cooldown_minutes"], 0)
    _int("daily_cap_minutes", cfg["daily_cap_minutes"], 0)
    _int("grace_seconds", cfg["grace_seconds"], 0, 3600)
    _number("loosen_delay_hours", cfg["loosen_delay_hours"], 0)

    warnings = cfg["warning_minutes"]
    if not isinstance(warnings, list):
        raise ConfigError("warning_minutes must be a list of whole numbers")
    cfg["warning_minutes"] = sorted({_int("warning_minutes", w, 1) for w in warnings}, reverse=True)

    reset = cfg["daily_reset_time"]
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", reset) if isinstance(reset, str) else None
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ConfigError(f'daily_reset_time must look like "04:00", got {reset!r}')
    cfg["daily_reset_time"] = f"{int(m[1]):02d}:{m[2]}"

    for key in ("exclude", "extra_paths"):
        cfg[key] = sorted({normalize_entry(e) for e in _str_list(key, cfg[key]) if e.strip()})

    return cfg


def _toml_value(value) -> str:
    if isinstance(value, str):
        return json.dumps(value)  # JSON string escapes are valid TOML basic strings
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return str(value)


_COMMENTS = {
    "session_minutes": "Maximum continuous play before a cooldown starts.",
    "cooldown_minutes": "Block length once the session is used up; also the break that refills it.",
    "daily_cap_minutes": "Total play per day. 0 = no daily cap.",
    "daily_reset_time": "When the daily cap resets (24h clock, local time).",
    "warning_minutes": "Toast warnings this many minutes before the limit.",
    "grace_seconds": "After asking games to close, wait this long before force-closing them.",
    "loosen_delay_hours": "How long looser settings (and uninstalling) take to apply.",
    "exclude": "Exe names or paths under game folders that should NOT count as games.",
    "extra_paths": "Extra folders whose programs count as games (non-Steam games).",
}


def render_config(cfg: dict) -> str:
    """Write settings back out as TOML (used by the settings window)."""
    lines = [
        "# PlayLimiter configuration (written by the settings window).",
        "# Stricter changes apply right away. Looser ones wait loosen_delay_hours.",
    ]
    for key in DEFAULTS:
        lines += ["", f"# {_COMMENTS[key]}", f"{key} = {_toml_value(cfg[key])}"]
    return "\n".join(lines) + "\n"
