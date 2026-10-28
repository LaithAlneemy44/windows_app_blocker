"""curfew command line."""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import datetime, timedelta

from playlimiter import rules as pl_rules
from playlimiter.notify import fmt_duration

from . import rules, store, winsys


def cmd_status(_args) -> int:
    from .daemon import load_state

    state = load_state()
    print(f"Daemon running: {'yes' if winsys.daemon_running() else 'NO'}")
    print(f"Logon task:     {'installed' if winsys.task_exists() else 'not installed'}")
    if state is None:
        print("No state yet. Run 'curfew install'.")
        return 0
    now = datetime.now()
    cfg = state.active
    st = rules.evaluate(state, now)
    if st.blocked:
        print(f"Curfew active:  apps allowed again at {cfg['wake_time']} (in {fmt_duration((st.ends_at - now).total_seconds())})")
    else:
        note = " (changed for today)" if st.changed else ""
        print(f"Tonight:        apps close at {st.bedtime}{note}, in {fmt_duration((st.starts_at - now).total_seconds())}")
        reason = rules.later_refusal(state, now)
        print(f"                {reason or 'Can be made later once, until ' + cfg['change_deadline'] + '.'}")

    print("Active settings:")
    for k in rules.DEFAULTS:
        print(f"  {k} = {cfg[k]!r}")
    if state.pending:
        print("Pending (looser) changes:")
        for p in sorted(state.pending, key=lambda p: p["remaining_s"]):
            when = now + timedelta(seconds=p["remaining_s"])
            print(f"  {pl_rules.describe_change(p):40} applies {when:%a %d %b %H:%M}")
    else:
        print("Pending changes: none")
    if state.uninstall_remaining_s is not None:
        when = now + timedelta(seconds=state.uninstall_remaining_s)
        print(f"UNINSTALL scheduled for {when:%a %d %b %H:%M} (curfew cancel-uninstall to cancel)")
    return 0


def cmd_install(_args) -> int:
    if store.ensure_config():
        print(f"Created {store.config_path()} from config.example.toml")
    else:
        print(f"Using existing {store.config_path()}")
    winsys.register_task()
    print(f"Registered logon task '{winsys.TASK_NAME}'")
    if winsys.daemon_running():
        print("Browser Curfew is already running.")
    else:
        winsys.run_task()
        print("Started Browser Curfew in the background.")
    print(f"Desktop shortcut: {winsys.create_shortcut()}")
    return 0


def cmd_shortcut(_args) -> int:
    print(f"Desktop shortcut: {winsys.create_shortcut()}")
    return 0


def send_request(req: dict) -> tuple[bool, str]:
    """Hand a request to the running daemon, or apply it directly if it isn't running.

    Returns (ok, message for the user). Also used by the settings window.
    """
    from .daemon import apply_request, load_state, save_state

    if winsys.daemon_running():
        d = store.requests_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{uuid.uuid4().hex}.json"
        reply = path.with_suffix(".reply")
        store.write_json_atomic(path, req)
        for _ in range(50):
            if reply.exists():
                data = store.read_json(reply)
                reply.unlink(missing_ok=True)
                return data["ok"], data["message"]
            if not path.exists():  # consumed without a reply: the daemon rejected it
                return False, "Browser Curfew couldn't handle that request; see curfew.log."
            time.sleep(0.1)
        return False, "Request queued, but Browser Curfew hasn't picked it up yet."

    state = load_state()
    if state is None:
        return False, "Browser Curfew has no state yet. Run 'curfew install'."
    ok, message = apply_request(state, req)
    save_state(state)
    if req["cmd"] == "uninstall":
        message += "\nNote: Browser Curfew is not running. Removal happens once it runs (at next logon)."
    return ok, message


def _send(req: dict) -> int:
    ok, message = send_request(req)
    print(message)
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="curfew", description="Close browsers, chat apps and games at night.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show tonight's curfew, active settings and pending changes")
    sub.add_parser("install", help="register the logon task and start the app")
    tonight = sub.add_parser("tonight", help="change tonight's curfew time (later: once a day, before the deadline)")
    tonight.add_argument("time", help='24h time, e.g. "23:30"')
    sub.add_parser("uninstall", help="schedule removal after loosen_delay_hours")
    sub.add_parser("cancel-uninstall", help="cancel a scheduled removal")
    sub.add_parser("settings", help="open the settings window")
    sub.add_parser("shortcut", help="(re)create the desktop shortcut to the settings window")
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
    if args.cmd == "tonight":
        return _send({"cmd": "tonight", "time": args.time})
    return _send({"cmd": args.cmd})


if __name__ == "__main__":
    sys.exit(main())
