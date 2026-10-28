"""Background loop: find browsers, chat apps and games, check the curfew, warn, close them."""
from __future__ import annotations

import json
import logging
import math
import shutil
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from playlimiter import overlay, winsys as pl_winsys
from playlimiter import rules as pl_rules
from playlimiter.config import ConfigError
from playlimiter.enforce import Closer
from playlimiter.notify import set_app_name, toast

from . import rules, store, winsys
from .config import parse_config, parse_time
from .detect import AppScanner

log = logging.getLogger("curfew")

POLL_S = 2.0
SAVE_EVERY_S = 10.0
BLOCKED_LAUNCH_GRACE_S = 10
WARNING_BANNER_S = 10
BLOCKED_BANNER_MIN_S = 8
BLOCKED_NOTICE_EVERY_S = 60  # don't repeat the "blocked" notice for every relaunch


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


def apply_request(state: rules.State, req: dict) -> tuple[bool, str]:
    """Handle a CLI/settings-window request against the state. Returns (ok, message for the user)."""
    cmd = req["cmd"]
    if cmd == "tonight":
        try:
            return True, rules.set_tonight(state, datetime.now(), parse_time("time", req["time"]))
        except (ConfigError, rules.CurfewError) as e:
            return False, str(e)
    if cmd == "uninstall":
        remaining = pl_rules.request_uninstall(state)
        when = datetime.now() + timedelta(seconds=remaining)
        return True, f"Uninstall scheduled for {when:%a %d %b %H:%M}. Run 'curfew cancel-uninstall' to cancel."
    if cmd == "cancel-uninstall":
        return True, "Uninstall cancelled." if pl_rules.cancel_uninstall(state) else "No uninstall was scheduled."
    raise ValueError(f"unknown request {cmd!r}")


def perform_uninstall() -> None:
    log.warning("Uninstall delay elapsed: removing scheduled task and data")
    winsys.delete_task()
    winsys.remove_shortcut()
    toast("Browser Curfew removed", "The logon task and data were deleted.")
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
        self.scanner = AppScanner()
        self.closer = Closer()
        self.config_mtime: float | None = None
        self.last_save = 0.0
        self.last_blocked_notice = -BLOCKED_NOTICE_EVERY_S

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
            toast("Browser Curfew: config error", f"{e}. Keeping current settings.")
            return False
        result = pl_rules.reconcile(self.state, desired, rules.looser_rules(self.state.active), rules.LIST_LOOSE_OP)
        if result.tightened:
            log.info("Applied immediately: %s", "; ".join(result.tightened))
            toast("Browser Curfew: stricter settings active", "; ".join(result.tightened))
        for p in result.new_pending:
            when = datetime.now() + timedelta(seconds=p["remaining_s"])
            log.info("Pending until %s: %s", when, pl_rules.describe_change(p))
        if result.new_pending:
            when = datetime.now() + timedelta(seconds=max(p["remaining_s"] for p in result.new_pending))
            toast(
                "Browser Curfew: looser settings pending",
                f"{'; '.join(pl_rules.describe_change(p) for p in result.new_pending)} — takes effect {when:%a %d %b %H:%M}",
            )
        return bool(result.tightened or result.new_pending)

    def check_requests(self) -> bool:
        d = store.requests_dir()
        if not d.is_dir():
            return False
        changed = False
        for f in sorted(d.glob("*.json")):
            try:
                req = json.loads(f.read_text(encoding="utf-8"))
                ok, msg = apply_request(self.state, req)
                log.info("Request %s: %s", req, msg)
                if ok:
                    toast("Browser Curfew", msg)
                changed = True
                self.save()  # before replying, so the sender sees the new state
                store.write_json_atomic(f.with_suffix(".reply"), {"ok": ok, "message": msg})
            except Exception as e:
                log.error("Bad request file %s: %s", f.name, e)
            f.unlink(missing_ok=True)
        return changed

    # ------------------------------------------------------------ loop

    def save(self) -> None:
        save_state(self.state)
        self.last_save = time.monotonic()

    def handle_tick(self, result: rules.TickResult, apps: dict[int, str]) -> None:
        for p in result.applied:
            log.info("Pending change now active: %s", pl_rules.describe_change(p))
            toast("Browser Curfew: setting changed", f"Now active: {pl_rules.describe_change(p)}")

        cfg = self.state.active
        self.scanner.configure(cfg["apps"], cfg["close_games"])
        status = result.status

        if result.warnings:
            # The real time left, not the threshold crossed: tonight may have just been moved
            # to a few minutes away, which crosses the 15-minute threshold with far less left.
            mins = max(1, math.ceil((status.starts_at - datetime.now()).total_seconds() / 60))
            left = f"Apps close in {mins} minute{'s' if mins != 1 else ''}"
            body = f"Curfew at {status.bedtime}. Finish up and save your work."
            toast("Browser Curfew", f"{left}. {body}", sound=True)
            overlay.show_async(left, body, WARNING_BANNER_S)

        mono = pl_winsys.uptime_s()
        if status.blocked:
            new = [pid for pid in apps if not self.closer.is_closing(pid)]
            # Curfew just started with apps open: the full grace to save.
            # Launched during the curfew: nothing to save.
            ending_now = result.just_blocked
            grace = cfg["grace_minutes"] * 60
            grace = grace if ending_now else min(grace, BLOCKED_LAUNCH_GRACE_S)
            if new and (ending_now or time.monotonic() - self.last_blocked_notice >= BLOCKED_NOTICE_EVERY_S):
                self.last_blocked_notice = time.monotonic()
                again = f"Apps are allowed again at {cfg['wake_time']}."
                if ending_now:
                    why = "Curfew: save your work now"
                    body = f"Apps force-close in {grace // 60} min, so save anything you have open."
                    banner = "Save anything you have open before apps force-close.\nClosing in {clock}. " + again
                else:
                    why = "Curfew is on"
                    body = f"Closing the app in {grace} s."
                    banner = again
                toast(f"Browser Curfew: {why}", f"{body} {again}", sound=True)
                overlay.show_async(why, banner, max(grace, BLOCKED_BANNER_MIN_S))
            for pid in new:
                self.closer.close(pid, apps[pid], mono, grace)
        self.closer.step(mono)

    def run(self) -> None:
        set_app_name("Browser Curfew")
        r = self.engine.start(time.time(), pl_winsys.uptime_s(), datetime.now())
        log.info("Daemon started; status %s", r.status)
        self.check_config()
        self.scanner.configure(self.state.active["apps"], self.state.active["close_games"])
        self.save()
        while True:
            events = self.check_config() | self.check_requests()
            apps = self.scanner.scan()
            result = self.engine.tick(time.time(), pl_winsys.uptime_s(), datetime.now(), in_use=bool(apps))
            self.handle_tick(result, apps)
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
        log.info("Another Browser Curfew instance is running; exiting")
        return 0
    try:
        Daemon().run()
    except Exception:
        log.exception("Daemon crashed")
        raise
    return 0
