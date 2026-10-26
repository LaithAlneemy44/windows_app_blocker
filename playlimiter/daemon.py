"""Background loop: detect games, advance the rules, enforce, notify."""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from . import rules, store, winsys
from .config import ConfigError, parse_config
from .detect import GameScanner
from .enforce import Closer
from .notify import fmt_duration, toast

log = logging.getLogger("playlimiter")

POLL_S = 2.0
SAVE_EVERY_S = 10.0


def setup_logging() -> None:
    store.data_dir().mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(store.log_path(), maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def load_state() -> rules.State | None:
    try:
        data = store.read_json(store.state_path())
    except (ValueError, OSError) as e:
        log.error("state.json unreadable (%s); starting fresh", e)
        return None
    return rules.State.from_dict(data) if data else None


def save_state(state: rules.State) -> None:
    store.write_json_atomic(store.state_path(), state.to_dict())


def read_config() -> dict:
    return parse_config(store.config_path().read_text(encoding="utf-8"))


def apply_request(state: rules.State, cmd: str) -> str:
    """Handle a CLI command against the state. Returns a message for the user."""
    if cmd == "uninstall":
        remaining = rules.request_uninstall(state)
        when = datetime.now() + timedelta(seconds=remaining)
        return f"Uninstall scheduled for {when:%a %d %b %H:%M}. Run 'playlimiter cancel-uninstall' to cancel."
    if cmd == "cancel-uninstall":
        return "Uninstall cancelled." if rules.cancel_uninstall(state) else "No uninstall was scheduled."
    raise ValueError(f"unknown request {cmd!r}")


def perform_uninstall() -> None:
    log.warning("Uninstall delay elapsed: removing scheduled task and data")
    winsys.delete_task()
    toast("PlayLimiter removed", "The logon task and data were deleted. Run 'pip uninstall playlimiter' to remove the program.")
    logging.shutdown()
    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)
    shutil.rmtree(store.data_dir(), ignore_errors=True)


class Daemon:
    def __init__(self):
        store.ensure_config()
        state = load_state()
        if state is None:
            try:
                active = read_config()
            except (ConfigError, OSError) as e:
                log.error("config.toml invalid on first run (%s); using defaults", e)
                active = rules.DEFAULTS
            state = rules.State(active=json.loads(json.dumps(active)))
        self.state = state
        self.engine = rules.Engine(state)
        self.scanner = GameScanner()
        self.closer = Closer()
        self.config_mtime: float | None = None
        self.last_save = 0.0

    # ------------------------------------------------------------ config

    def check_config(self) -> bool:
        try:
            mtime = store.config_path().stat().st_mtime
        except OSError:
            return False
        if mtime == self.config_mtime:
            return False
        self.config_mtime = mtime
        try:
            desired = read_config()
        except (ConfigError, OSError, UnicodeDecodeError) as e:
            log.error("config.toml rejected, keeping active settings: %s", e)
            toast("PlayLimiter: config error", f"{e}. Keeping current settings.")
            return False
        result = rules.reconcile(self.state, desired)
        if result.tightened:
            log.info("Applied immediately: %s", "; ".join(result.tightened))
            toast("PlayLimiter: stricter settings active", "; ".join(result.tightened))
        for p in result.new_pending:
            when = datetime.now() + timedelta(seconds=p["remaining_s"])
            log.info("Pending until %s: %s", when, rules.describe_change(p))
        if result.new_pending:
            when = datetime.now() + timedelta(seconds=max(p["remaining_s"] for p in result.new_pending))
            toast(
                "PlayLimiter: looser settings pending",
                f"{'; '.join(rules.describe_change(p) for p in result.new_pending)} — takes effect {when:%a %d %b %H:%M}",
            )
        return bool(result.tightened or result.new_pending)

    def check_requests(self) -> bool:
        d = store.requests_dir()
        if not d.is_dir():
            return False
        changed = False
        for f in sorted(d.glob("*.json")):
            try:
                cmd = json.loads(f.read_text(encoding="utf-8"))["cmd"]
                msg = apply_request(self.state, cmd)
                log.info("Request %s: %s", cmd, msg)
                toast("PlayLimiter", msg)
                changed = True
            except Exception as e:
                log.error("Bad request file %s: %s", f.name, e)
            f.unlink(missing_ok=True)
        return changed

    # ------------------------------------------------------------ loop

    def save(self) -> None:
        save_state(self.state)
        self.last_save = time.monotonic()

    def handle_tick(self, result: rules.TickResult, games: dict[int, str]) -> None:
        for p in result.applied:
            log.info("Pending change now active: %s", rules.describe_change(p))
            toast("PlayLimiter: setting changed", f"Now active: {rules.describe_change(p)}")

        cfg = self.state.active
        self.scanner.configure(cfg["exclude"], cfg["extra_paths"])

        for w in result.warnings:
            toast("PlayLimiter", f"{w} minute{'s' if w != 1 else ''} of play left. Save your game now.")

        status = result.status
        mono = winsys.uptime_s()
        if status.blocked:
            new = [pid for pid in games if not self.closer.is_closing(pid)]
            if new:
                why = "Daily limit reached" if status.reason == "daily_cap" else "Time's up"
                if not result.just_blocked:
                    why = "Games are blocked"
                toast(
                    f"PlayLimiter: {why}",
                    f"Closing your game in {cfg['grace_seconds']} s, so save now. "
                    f"Play allowed again in {fmt_duration(status.wait_s)}.",
                )
            for pid in new:
                self.closer.close(pid, games[pid], mono, cfg["grace_seconds"])
        self.closer.step(mono)

    def run(self) -> None:
        r = self.engine.start(time.time(), winsys.uptime_s(), datetime.now())
        log.info("Daemon started; status %s", r.status)
        self.check_config()
        self.scanner.configure(self.state.active["exclude"], self.state.active["extra_paths"])
        self.save()
        while True:
            events = self.check_config() | self.check_requests()
            games = self.scanner.scan()
            result = self.engine.tick(time.time(), winsys.uptime_s(), datetime.now(), playing=bool(games))
            self.handle_tick(result, games)
            if result.uninstall_due:
                perform_uninstall()
                return
            events |= bool(result.applied or result.warnings or result.just_blocked)
            if events or time.monotonic() - self.last_save >= SAVE_EVERY_S:
                self.save()
            time.sleep(POLL_S)


def main() -> int:
    setup_logging()
    mutex = winsys.acquire_single_instance()
    if mutex is None:
        log.info("Another PlayLimiter instance is running; exiting")
        return 0
    try:
        Daemon().run()
    except Exception:
        log.exception("Daemon crashed")
        raise
    return 0
