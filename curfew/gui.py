"""Browser Curfew settings window.

Top: change just tonight's curfew (applied by the daemon with the once-a-day rules).
Bottom: the nightly defaults in config.toml (applied with the usual tighten/loosen rules).
"""
from __future__ import annotations

import copy
import ctypes
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from playlimiter import rules as pl_rules
from playlimiter.config import ConfigError
from playlimiter.gui import _fmt, _when
from playlimiter.notify import fmt_duration

from . import rules, store, winsys
from .config import parse_config, parse_time, render_config
from .daemon import load_state

TITLE = "Browser Curfew"
REFRESH_MS = 2000

# key, label, hint, type
SCALAR_FIELDS = [
    ("bedtime", "Curfew time", "Browsers close at this time every night (24h, e.g. 22:30)", str),
    ("wake_time", "Allowed again at", "When the curfew lifts each morning", str),
    ("change_deadline", "Change deadline", "Tonight can only be made later before this", str),
    ("warning_minutes", "Warnings (minutes before)", "Comma-separated, e.g. 15, 5", "list"),
    ("grace_minutes", "Time to save (minutes)", "Banner countdown before browsers are force-closed", int),
    ("loosen_delay_hours", "Loosening delay (hours)", "How long looser settings wait", float),
]
LIST_FIELDS = [
    ("browsers", "Browsers to close", "Exe names (chrome.exe) or full paths, one per line"),
]


def _time_choices(wake_time: str) -> list[str]:
    """Every 15 minutes from 18:00 up to the wake time."""
    out = []
    t = datetime(2000, 1, 1, 18, 0)
    for _ in range(24 * 4):
        hhmm = f"{t:%H:%M}"
        if out and rules.since_wake(hhmm, wake_time) < rules.since_wake(out[-1], wake_time):
            break  # wrapped past the wake time
        out.append(hhmm)
        t += timedelta(minutes=15)
    return out


class SettingsWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"{TITLE} Settings")
        try:
            root.iconbitmap(default=str(winsys.ICON_PATH))
        except tk.TclError:
            pass
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=14)
        frame.grid(sticky="nsew")

        self.status_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.status_var, font=("Segoe UI", 10, "bold"), justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )

        # ---- tonight only
        tonight = ttk.LabelFrame(frame, text="Tonight only", padding=10)
        tonight.grid(row=1, column=0, columnspan=3, sticky="we", pady=(0, 12))
        ttk.Label(tonight, text="Browsers close tonight at").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.tonight_var = tk.StringVar()
        self.tonight_box = ttk.Combobox(tonight, textvariable=self.tonight_var, width=8)
        self.tonight_box.grid(row=0, column=1, sticky="w")
        ttk.Button(tonight, text="Set for tonight", command=self.set_tonight).grid(row=0, column=2, padx=(10, 0))
        self.tonight_hint = ttk.Label(tonight, foreground="#666", justify="left", wraplength=520)
        self.tonight_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---- nightly defaults
        defaults = ttk.LabelFrame(frame, text="Every night (default)", padding=10)
        defaults.grid(row=2, column=0, columnspan=3, sticky="we")
        self.vars: dict[str, tk.StringVar] = {}
        self.active_labels: dict[str, ttk.Label] = {}
        row = 0
        for key, label, hint, _ in SCALAR_FIELDS:
            ttk.Label(defaults, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10))
            var = tk.StringVar()
            ttk.Entry(defaults, textvariable=var, width=14).grid(row=row, column=1, sticky="w")
            ttk.Label(defaults, text=hint, foreground="#666").grid(row=row, column=2, sticky="w", padx=(10, 0))
            self.vars[key] = var
            self.active_labels[key] = ttk.Label(defaults, foreground="#666", font=("Segoe UI", 8))
            self.active_labels[key].grid(row=row + 1, column=1, columnspan=2, sticky="w", pady=(0, 6))
            row += 2

        self.texts: dict[str, tk.Text] = {}
        for key, label, hint in LIST_FIELDS:
            ttk.Label(defaults, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=(6, 0))
            box = ttk.Frame(defaults)
            box.grid(row=row, column=1, columnspan=2, sticky="we", pady=(6, 0))
            text = tk.Text(box, width=58, height=5, font=("Segoe UI", 9))
            text.grid(row=0, column=0, sticky="we")
            ttk.Button(box, text="Add exe…", command=self.add_exe).grid(row=0, column=1, sticky="n", padx=(6, 0))
            ttk.Label(defaults, text=hint, foreground="#666").grid(row=row + 1, column=1, columnspan=2, sticky="w")
            self.texts[key] = text
            self.active_labels[key] = ttk.Label(defaults, foreground="#666", font=("Segoe UI", 8), wraplength=520)
            self.active_labels[key].grid(row=row + 2, column=1, columnspan=2, sticky="w", pady=(0, 6))
            row += 3

        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(actions, text="Reload", command=self.load_form).grid(row=0, column=0, padx=4)
        ttk.Button(actions, text="Save defaults", command=self.save).grid(row=0, column=1, padx=4)
        ttk.Button(actions, text="Close", command=root.destroy).grid(row=0, column=2, padx=4)

        self.load_form()
        self.refresh()
        self.load_tonight()

    # ------------------------------------------------------------ tonight

    def load_tonight(self) -> None:
        state = load_state()
        cfg = state.active if state else rules.DEFAULTS
        self.tonight_box.configure(values=_time_choices(cfg["wake_time"]))
        self.tonight_var.set(rules.evaluate(state, datetime.now()).bedtime if state else cfg["bedtime"])

    def set_tonight(self) -> None:
        try:
            time = parse_time("Tonight's time", self.tonight_var.get())
        except ConfigError as e:
            messagebox.showerror(TITLE, str(e))
            return
        state = load_state()
        if state is None:
            messagebox.showerror(TITLE, "Browser Curfew hasn't run yet. Run 'curfew install' first.")
            return
        now = datetime.now()
        st = rules.evaluate(state, now)
        wake = state.active["wake_time"]
        later = rules.since_wake(time, wake) > rules.since_wake(st.bedtime, wake)
        if later:
            reason = rules.later_refusal(state, now)
            if reason:
                messagebox.showerror(TITLE, reason)
                return
            question = (
                f"Make tonight's curfew {time} instead of {st.bedtime}?\n\n"
                "This uses today's one change: after this, tonight can only be made earlier.\n"
                f"Tomorrow it goes back to {state.active['bedtime']}."
            )
        elif time != st.bedtime:
            question = f"Move tonight's curfew earlier, to {time}?"
            if now >= st.deadline or st.change_used:
                question += "\n\nYou won't be able to make it later again today."
        else:
            messagebox.showinfo(TITLE, f"Tonight's curfew is already {time}.")
            return
        if not messagebox.askyesno("Change tonight's curfew?", question):
            return
        from .cli import send_request

        ok, message = send_request({"cmd": "tonight", "time": time})
        (messagebox.showinfo if ok else messagebox.showerror)(TITLE, message)
        self.load_tonight()
        self.root.after(200, self.refresh)

    # ------------------------------------------------------------ form

    def load_form(self) -> None:
        store.ensure_config()
        try:
            cfg = parse_config(store.config_path().read_text(encoding="utf-8"))
        except (ConfigError, OSError) as e:
            state = load_state()
            cfg = state.active if state else rules.DEFAULTS
            messagebox.showwarning(TITLE, f"config.toml has a problem, showing active settings instead:\n\n{e}")
        for key, *_ in SCALAR_FIELDS:
            self.vars[key].set(_fmt(cfg[key]) if key != "warning_minutes" else ", ".join(map(str, cfg[key])))
        for key, *_ in LIST_FIELDS:
            self.texts[key].delete("1.0", "end")
            self.texts[key].insert("1.0", "\n".join(cfg[key]))

    def read_form(self) -> dict:
        cfg = {}
        for key, label, _, kind in SCALAR_FIELDS:
            raw = self.vars[key].get().strip()
            try:
                if kind == "list":
                    cfg[key] = [int(x) for x in raw.replace(",", " ").split()]
                elif kind is str:
                    cfg[key] = raw
                else:
                    value = float(raw)
                    cfg[key] = int(value) if kind is int or value.is_integer() else value
                    if kind is int and value != int(value):
                        raise ValueError
            except ValueError:
                raise ConfigError(f"{label}: {raw!r} is not a valid number") from None
        for key, *_ in LIST_FIELDS:
            cfg[key] = [line.strip() for line in self.texts[key].get("1.0", "end").splitlines() if line.strip()]
        return cfg

    def add_exe(self) -> None:
        path = filedialog.askopenfilename(title="Choose a browser to close", filetypes=[("Programs", "*.exe")])
        if path:
            has_text = bool(self.texts["browsers"].get("1.0", "end").strip())
            self.texts["browsers"].insert("end", ("\n" if has_text else "") + path.replace("/", "\\"))

    def save(self) -> None:
        try:
            text = render_config(self.read_form())
            desired = parse_config(text)  # same validation the daemon uses
        except ConfigError as e:
            messagebox.showerror(TITLE, str(e))
            return

        state = load_state()
        if state is not None:
            preview = copy.deepcopy(state)
            result = pl_rules.reconcile(preview, desired, rules.looser_rules(preview.active), rules.LIST_LOOSE_OP)
            lines = []
            if result.tightened:
                lines.append("Applies immediately:")
                lines += [f"  • {t}" for t in result.tightened]
            if preview.pending:
                lines.append(f"\nLooser, so they wait {_fmt(state.active['loosen_delay_hours'])} h:")
                lines += [
                    f"  • {pl_rules.describe_change(p)}  (from {_when(p['remaining_s'])})"
                    for p in sorted(preview.pending, key=lambda p: p["remaining_s"])
                ]
            dropped = [p for p in state.pending if p not in preview.pending and
                       not any(q["key"] == p["key"] and q.get("item") == p.get("item") for q in preview.pending)]
            if dropped:
                lines.append("\nCancelled pending changes:")
                lines += [f"  • {pl_rules.describe_change(p)}" for p in dropped]
            if not lines:
                messagebox.showinfo(TITLE, "Nothing to change.")
                return
            lines.append("\n(To change only tonight, use the Tonight only box instead.)")
            if not messagebox.askyesno("Save default settings?", "\n".join(lines) + "\n\nSave these settings?"):
                return

        store.write_text_atomic(store.config_path(), text)
        note = "" if winsys.daemon_running() else "\n\nNote: Browser Curfew isn't running, so nothing applies until it starts."
        messagebox.showinfo(TITLE, "Saved. Browser Curfew picks the change up within a few seconds." + note)
        self.root.after(3000, self.refresh)

    # ------------------------------------------------------------ status

    def refresh(self) -> None:
        state = load_state()
        running = winsys.daemon_running()
        if state is None:
            self.status_var.set("Browser Curfew hasn't run yet." if not running else "Starting…")
            self.tonight_hint.configure(text="")
        else:
            now = datetime.now()
            cfg = state.active
            st = rules.evaluate(state, now)
            if st.blocked:
                line = f"Curfew active: browsers allowed again at {cfg['wake_time']} (in {fmt_duration((st.ends_at - now).total_seconds())})"
            else:
                note = f" (changed for today; normally {cfg['bedtime']})" if st.changed else ""
                line = f"Tonight: browsers close at {st.bedtime}{note}, in {fmt_duration((st.starts_at - now).total_seconds())}"
            if state.uninstall_remaining_s is not None:
                line += f"\nUninstall scheduled for {_when(state.uninstall_remaining_s)}"
            if not running:
                line += "\n⚠ Browser Curfew is not running. Run 'curfew install' to start it."
            self.status_var.set(line)

            reason = rules.later_refusal(state, now)
            hint = reason or (
                f"You can make tonight later once today, until {cfg['change_deadline']}. "
                "Earlier works any time."
            )
            self.tonight_hint.configure(text=f"{hint} Tomorrow it's back to {cfg['bedtime']}.")

            for key, label in self.active_labels.items():
                text = f"Active: {_fmt(cfg[key])}"
                for p in state.pending:
                    if p["key"] == key:
                        change = _fmt(p["value"]) if "op" not in p else f"{p['op']} {p['item']}"
                        text += f"   →  {change} from {_when(p['remaining_s'])}"
                label.configure(text=text)
        self.root.after(REFRESH_MS, self.refresh)


def main() -> int:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    SettingsWindow(root)
    root.mainloop()
    return 0
