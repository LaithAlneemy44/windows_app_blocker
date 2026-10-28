"""Background loop: detect games, advance the rules, enforce, notify."""
from __future__ import annotations

import json
import logging
import math
import shutil
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from . import overlay, rules, store, winsys
from .config import ConfigError, parse_config
from .detect import GameScanner
from .enforce import Closer
from .notify import fmt_duration, toast

log = logging.getLogger("playlimiter")

POLL_S = 2.0
SAVE_EVERY_S = 10.0
BLOCKED_LAUNCH_GRACE_S = 10
WARNING_BANNER_S = 10
BLOCKED_BANNER_MIN_S = 8


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
    if cmd == "start-cooldown":
        if not rules.start_cooldown(state):
            return "A cooldown is already running."
        return f"Cooldown started. Games are blocked for {fmt_duration(state.active['cooldown_minutes'] * 60)}."
    raise ValueError(f"unknown request {cmd!r}")


def perform_uninstall() -> None:
    log.warning("Uninstall delay elapsed: removing scheduled task and data")
    winsys.delete_task()
    winsys.remove_shortcut()
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
        self.cooldown_requested = False  # started by hand: running games get the full grace

    # ------------------------------------------------------------ config

    def check_config(self) -> bool:
        try:
            mtime = store.config_path().stat().st_mtime
        except OSError:
            return False
        if mtime == self.config_mtime:
            return False
        try:
            text = store.config_path().read_text(encoding="utf-8")
        except OSError as e:
            # Probably mid-save by an editor or the settings window; try again next poll.
            log.debug("config.toml not readable yet: %s", e)
            return False
        self.config_mtime = mtime
        try:
            desired = parse_config(text)
        except (ConfigError, UnicodeDecodeError) as e:
            log.error("config.toml rejected, keeping active settings: %s", e)
            toast("PlayLimiter: config error", f"{e}. Keeping current settings.")
            return False
        result = rules.reconcile(self.state, desired)
        if result.immediate:
            log.info("Applied immediately: %s", "; ".join(result.immediate))
            toast("PlayLimiter: settings applied", "; ".join(result.immediate))
        for p in result.new_pending:
            when = datetime.now() + timedelta(seconds=p["remaining_s"])
            log.info("Pending until %s: %s", when, rules.describe_change(p))
        if result.new_pending:
            when = datetime.now() + timedelta(seconds=max(p["remaining_s"] for p in result.new_pending))
            toast(
                "PlayLimiter: looser settings pending",
                f"{'; '.join(rules.describe_change(p) for p in result.new_pending)} — takes effect {when:%a %d %b %H:%M}",
            )
        return bool(result.immediate or result.new_pending)

    def check_requests(self) -> bool:
        d = store.requests_dir()
        if not d.is_dir():
            return False
        changed = False
        for f in sorted(d.glob("*.json")):
            try:
                cmd = json.loads(f.read_text(encoding="utf-8"))["cmd"]
                was_cooling = self.state.cooldown_elapsed_s is not None
                msg = apply_request(self.state, cmd)
                if cmd == "start-cooldown" and not was_cooling:
                    self.cooldown_requested = True
                log.info("Request %s: %s", cmd, msg)
                toast("PlayLimiter", msg)
                changed = True
                self.save()  # before replying, so the sender sees the new state
                store.write_json_atomic(f.with_suffix(".reply"), {"message": msg})
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

        if result.warnings:
            # The real time left, not the threshold crossed (relaunching with 5 min left crosses "10").
            mins = max(1, math.ceil(result.status.remaining_s / 60))
            left = f"{mins} minute{'s' if mins != 1 else ''} of play left"
            toast("PlayLimiter", f"{left}. Save your game now.", sound=True)
            overlay.show_async(left, "Save your game now.", WARNING_BANNER_S)

        status = result.status
        mono = winsys.uptime_s()
        if status.blocked:
            new = [pid for pid in games if not self.closer.is_closing(pid)]
            # Time ran out (or cooldown started by hand) mid-game: full grace to save.
            # Launched while already blocked: nothing to save.
            ending_now = result.just_blocked or self.cooldown_requested
            grace = cfg["grace_seconds"] if ending_now else min(cfg["grace_seconds"], BLOCKED_LAUNCH_GRACE_S)
            if new:
                again = f"Play allowed again in {fmt_duration(status.wait_s)}."
                if ending_now:
                    if self.cooldown_requested:
                        why = "Cooldown started"
                    else:
                        why = "Daily limit reached" if status.reason == "daily_cap" else "Time's up"
                    body = f"Closing your game in {grace} s, so save now."
                    banner = "Save now. Your game closes in {left} s.\n" + again
                else:
                    why = "Games are blocked"
                    body = f"Closing the game in {grace} s."
                    banner = again
                toast(f"PlayLimiter: {why}", f"{body} {again}", sound=True)
                overlay.show_async(why, banner, max(grace, BLOCKED_BANNER_MIN_S))
            elif result.just_blocked and not games:
                # Session ran out after you'd already quit: just say when the break ends.
                until = datetime.now() + timedelta(seconds=status.wait_s)
                toast("PlayLimiter: session over", f"Break until {until:%H:%M}.")
            for pid in new:
                self.closer.close(pid, games[pid], mono, grace)
        self.cooldown_requested = False
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
            had_session = self.state.session_elapsed_s is not None
            result = self.engine.tick(time.time(), winsys.uptime_s(), datetime.now(), playing=bool(games))
            self.handle_tick(result, games)
            if result.uninstall_due:
                perform_uninstall()
                return
            session_changed = had_session != (self.state.session_elapsed_s is not None)
            events |= bool(result.applied or result.warnings or result.just_blocked or session_changed)
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
