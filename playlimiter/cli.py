"""playlimiter command line."""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timedelta

from . import rules, store, winsys
from .notify import fmt_duration


def cmd_status(_args) -> int:
    from .daemon import load_state

    state = load_state()
    print(f"Daemon running: {'yes' if winsys.daemon_running() else 'NO'}")
    print(f"Logon task:     {'installed' if winsys.task_exists() else 'not installed'}")
    if state is None:
        print("No state yet. Run 'playlimiter install'.")
        return 0
    now = datetime.now()
    cfg = state.active
    status = rules.evaluate(state, now)
    session_left = max(0.0, cfg["session_minutes"] * 60 - state.used_s)

    if status.reason == "cooldown":
        print(f"Cooldown:       {fmt_duration(status.wait_s)} remaining")
    elif status.reason == "daily_cap":
        print(f"Daily cap hit:  play allowed again in {fmt_duration(status.wait_s)}")
    else:
        print(f"Budget left:    {fmt_duration(session_left)} of {cfg['session_minutes']} min")
        if state.used_s > 0:
            refill = max(0.0, cfg["cooldown_minutes"] * 60 - state.idle_s)
            print(f"                refills after {fmt_duration(refill)} more without playing")
    cap = cfg["daily_cap_minutes"]
    used = fmt_duration(state.daily_used_s) if state.daily_used_s else "0 min"
    print(f"Today:          {used} played" + (f" of {cap} min cap" if cap else " (no daily cap)"))

    print("Active settings:")
    for k in rules.DEFAULTS:
        print(f"  {k} = {cfg[k]!r}")
    if state.pending:
        print("Pending (looser) changes:")
        for p in sorted(state.pending, key=lambda p: p["remaining_s"]):
            when = now + timedelta(seconds=p["remaining_s"])
            print(f"  {rules.describe_change(p):40} applies {when:%a %d %b %H:%M}")
    else:
        print("Pending changes: none")
    if state.uninstall_remaining_s is not None:
        when = now + timedelta(seconds=state.uninstall_remaining_s)
        print(f"UNINSTALL scheduled for {when:%a %d %b %H:%M} (playlimiter cancel-uninstall to cancel)")
    return 0


def cmd_install(_args) -> int:
    if store.ensure_config():
        print(f"Created {store.config_path()} from config.example.toml")
    else:
        print(f"Using existing {store.config_path()}")
    winsys.register_task(store.data_dir())
    print(f"Registered logon task '{winsys.TASK_NAME}'")
    if winsys.daemon_running():
        print("PlayLimiter is already running.")
    else:
        winsys.run_task()
        print("Started PlayLimiter in the background.")
    print(f"Desktop shortcut: {winsys.create_shortcut()}")
    return 0


def cmd_shortcut(_args) -> int:
    print(f"Desktop shortcut: {winsys.create_shortcut()}")
    return 0


def send_request(cmd: str) -> tuple[bool, str]:
    """Hand a command to the running daemon, or apply it directly if it isn't running.

    Returns (ok, message for the user). Also used by the settings window.
    """
    from .daemon import apply_request, load_state, save_state

    if winsys.daemon_running():
        d = store.requests_dir()
        d.mkdir(parents=True, exist_ok=True)
        req = d / f"{uuid.uuid4().hex}.json"
        reply = req.with_suffix(".reply")
        store.write_json_atomic(req, {"cmd": cmd})
        for _ in range(50):
            if reply.exists():
                message = store.read_json(reply)["message"]
                reply.unlink(missing_ok=True)
                return True, message
            if not req.exists():  # consumed without a reply: the daemon rejected it
                return False, "PlayLimiter couldn't handle that request; see playlimiter.log."
            time.sleep(0.1)
        return False, "Request queued, but PlayLimiter hasn't picked it up yet."

    state = load_state()
    if state is None:
        return False, "PlayLimiter has no state; nothing to do."
    message = apply_request(state, cmd)
    save_state(state)
    if cmd == "uninstall":
        message += "\nNote: PlayLimiter is not running. Removal happens once it runs (at next logon)."
    return True, message


def _send(cmd: str) -> int:
    ok, message = send_request(cmd)
    print(message)
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="playlimiter", description="Limit how long you play games.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show budget, cooldown, daily usage and pending changes")
    sub.add_parser("install", help="register the logon task and start the app")
    sub.add_parser("uninstall", help="schedule removal after loosen_delay_hours")
    sub.add_parser("cancel-uninstall", help="cancel a scheduled removal")
    sub.add_parser("start-cooldown", help="start the cooldown now, even with play time left")
    sub.add_parser("settings", help="open the settings window")
    sub.add_parser("shortcut", help="(re)create the desktop shortcut to the settings window")
    overlay = sub.add_parser("overlay", help=argparse.SUPPRESS)  # used by the daemon
    overlay.add_argument("--seconds", type=int, default=10)
    overlay.add_argument("title")
    overlay.add_argument("message")
    sub.add_parser("run", help="run the background loop (used by the logon task)")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        from .daemon import main as daemon_main

        return daemon_main()
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "install":
        return cmd_install(args)
    if args.cmd == "settings":
        from .gui import main as gui_main

        return gui_main()
    if args.cmd == "shortcut":
        return cmd_shortcut(args)
    if args.cmd == "overlay":
        from .overlay import show

        show(args.title, args.message, args.seconds)
        return 0
    return _send(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
