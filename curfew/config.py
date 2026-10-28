"""Parse and validate Browser Curfew's config.toml into a plain dict (pure, no file I/O)."""
from __future__ import annotations

import copy
import re
import tomllib

from playlimiter.config import ConfigError, _int, _number, _str_list, _toml_value, normalize_entry

from .rules import DEFAULTS


def parse_time(name: str, value) -> str:
    """Validate a 24h "HH:MM" time and normalise it to two-digit hours."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip()) if isinstance(value, str) else None
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ConfigError(f'{name} must be a 24h time like "22:30", got {value!r}')
    return f"{int(m[1]):02d}:{m[2]}"


def parse_config(text: str) -> dict:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML: {e}") from e

    # "browsers" was renamed to "apps" when chat apps and games were added.
    if "browsers" in raw:
        if "apps" in raw:
            raise ConfigError('use "apps" only ("browsers" is its old name)')
        raw["apps"] = raw.pop("browsers")

    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(sorted(unknown))}")

    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(raw)

    for key in ("bedtime", "wake_time", "change_deadline"):
        cfg[key] = parse_time(key, cfg[key])
    if cfg["bedtime"] == cfg["wake_time"]:
        raise ConfigError("bedtime and wake_time can't be the same")
    _int("grace_minutes", cfg["grace_minutes"], 0, 60)
    _number("loosen_delay_hours", cfg["loosen_delay_hours"], 0)

    warnings = cfg["warning_minutes"]
    if not isinstance(warnings, list):
        raise ConfigError("warning_minutes must be a list of whole numbers")
    cfg["warning_minutes"] = sorted({_int("warning_minutes", w, 1) for w in warnings}, reverse=True)

    if not isinstance(cfg["close_games"], bool):
        raise ConfigError(f"close_games must be true or false, got {cfg['close_games']!r}")
    cfg["apps"] = sorted({normalize_entry(e) for e in _str_list("apps", cfg["apps"]) if e.strip()})
    return cfg


_COMMENTS = {
    "bedtime": "Apps close at this time every night (24h clock).",
    "wake_time": "Apps are allowed again from this time each morning.",
    "change_deadline": "Tonight's curfew can only be made later (once per day) before this time.",
    "warning_minutes": "Warnings this many minutes before the curfew.",
    "grace_minutes": "At the curfew, a banner counts down this long before apps are force-closed.",
    "loosen_delay_hours": "How long looser settings (and uninstalling) take to apply.",
    "close_games": "Also close games (Steam games, found the same way PlayLimiter finds them).",
    "apps": "Programs to close: exe names (chrome.exe) or full paths.",
}


def render_config(cfg: dict) -> str:
    """Write settings back out as TOML (used by the settings window)."""
    lines = [
        "# Browser Curfew configuration (written by the settings window).",
        "# Stricter changes apply right away. Looser ones wait loosen_delay_hours.",
    ]
    for key in DEFAULTS:
        lines += ["", f"# {_COMMENTS[key]}", f"{key} = {_toml_value(cfg[key])}"]
    return "\n".join(lines) + "\n"
