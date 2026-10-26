"""Settings window. Edits config.toml; the daemon applies it with the usual tighten/loosen rules."""
from __future__ import annotations

import copy
import ctypes
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from . import rules, store, winsys
from .config import ConfigError, parse_config, render_config
from .daemon import load_state
from .notify import fmt_duration

REFRESH_MS = 2000

# key, label, hint, type
SCALAR_FIELDS = [
    ("session_minutes", "Session length (minutes)", "Starts at game launch; keeps running if you quit", int),
    ("cooldown_minutes", "Break / cooldown (minutes)", "Break after each session; games are blocked", int),
    ("daily_cap_minutes", "Daily cap (minutes)", "0 = no daily cap", int),
    ("daily_reset_time", "Daily reset time", "24h clock, e.g. 04:00", str),
    ("warning_minutes", "Warnings (minutes before)", "Comma-separated, e.g. 10, 2", "list"),
    ("grace_seconds", "Grace period (seconds)", "Time to save before a game is force-closed", int),
    ("loosen_delay_hours", "Loosening delay (hours)", "How long looser settings wait", float),
]
LIST_FIELDS = [
    ("exclude", "Don't count as games", "Exe names (wallpaper64.exe) or paths, one per line"),
    ("extra_paths", "Also count as games", "Folders of non-Steam games, one per line"),
]


def _when(seconds: float) -> str:
    return f"{datetime.now() + timedelta(seconds=seconds):%a %d %b %H:%M}"


def _clock(seconds: float) -> str:
    return f"{datetime.now() + timedelta(seconds=seconds):%H:%M}"


def _fmt(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(none)"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class SettingsWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("PlayLimiter Settings")
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

        self.vars: dict[str, tk.StringVar] = {}
        self.active_labels: dict[str, ttk.Label] = {}
        row = 1
        for key, label, hint, _ in SCALAR_FIELDS:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10))
            var = tk.StringVar()
            ttk.Entry(frame, textvariable=var, width=14).grid(row=row, column=1, sticky="w")
            ttk.Label(frame, text=hint, foreground="#666").grid(row=row, column=2, sticky="w", padx=(10, 0))
            self.vars[key] = var
            self.active_labels[key] = ttk.Label(frame, foreground="#666", font=("Segoe UI", 8))
            self.active_labels[key].grid(row=row + 1, column=1, columnspan=2, sticky="w", pady=(0, 6))
            row += 2

        self.texts: dict[str, tk.Text] = {}
        for key, label, hint in LIST_FIELDS:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=(6, 0))
            box = ttk.Frame(frame)
            box.grid(row=row, column=1, columnspan=2, sticky="we", pady=(6, 0))
            text = tk.Text(box, width=58, height=3, font=("Segoe UI", 9))
            text.grid(row=0, column=0, sticky="we")
            buttons = ttk.Frame(box)
            buttons.grid(row=0, column=1, sticky="n", padx=(6, 0))
            if key == "exclude":
                ttk.Button(buttons, text="Add exe…", command=self.add_exe).grid(sticky="we")
            ttk.Button(buttons, text="Add folder…", command=lambda k=key: self.add_folder(k)).grid(sticky="we")
            ttk.Label(frame, text=hint, foreground="#666").grid(row=row + 1, column=1, columnspan=2, sticky="w")
            self.texts[key] = text
            self.active_labels[key] = ttk.Label(frame, foreground="#666", font=("Segoe UI", 8))
            self.active_labels[key].grid(row=row + 2, column=1, columnspan=2, sticky="w", pady=(0, 6))
            row += 3

        self.cooldown_button = ttk.Button(frame, text="Start cooldown now", command=self.start_cooldown)
        self.cooldown_button.grid(row=row, column=0, sticky="w", pady=(10, 0))
        actions = ttk.Frame(frame)
        actions.grid(row=row, column=1, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(actions, text="Reload", command=self.load_form).grid(row=0, column=0, padx=4)
        ttk.Button(actions, text="Save", command=self.save).grid(row=0, column=1, padx=4)
        ttk.Button(actions, text="Close", command=root.destroy).grid(row=0, column=2, padx=4)

        self.load_form()
        self.refresh()

    # ------------------------------------------------------------ form

    def load_form(self) -> None:
        store.ensure_config()
        try:
            cfg = parse_config(store.config_path().read_text(encoding="utf-8"))
        except (ConfigError, OSError) as e:
            state = load_state()
            cfg = state.active if state else rules.DEFAULTS
            messagebox.showwarning("PlayLimiter", f"config.toml has a problem, showing active settings instead:\n\n{e}")
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
        path = filedialog.askopenfilename(title="Choose a program to exclude", filetypes=[("Programs", "*.exe")])
        if path:
            self.texts["exclude"].insert("end", ("\n" if self._has_text("exclude") else "") + path.replace("/", "\\"))

    def add_folder(self, key: str) -> None:
        path = filedialog.askdirectory(title="Choose a folder")
        if path:
            self.texts[key].insert("end", ("\n" if self._has_text(key) else "") + path.replace("/", "\\"))

    def _has_text(self, key: str) -> bool:
        return bool(self.texts[key].get("1.0", "end").strip())

    # ------------------------------------------------------------ save

    def save(self) -> None:
        try:
            text = render_config(self.read_form())
            desired = parse_config(text)  # same validation the daemon uses
        except ConfigError as e:
            messagebox.showerror("PlayLimiter", str(e))
            return

        state = load_state()
        if state is not None:
            preview = copy.deepcopy(state)
            result = rules.reconcile(preview, desired)
            lines = []
            if result.tightened:
                lines.append("Applies immediately:")
                lines += [f"  • {t}" for t in result.tightened]
            if preview.pending:
                lines.append(f"\nLooser, so they wait {_fmt(state.active['loosen_delay_hours'])} h:")
                lines += [
                    f"  • {rules.describe_change(p)}  (from {_when(p['remaining_s'])})"
                    for p in sorted(preview.pending, key=lambda p: p["remaining_s"])
                ]
            dropped = [p for p in state.pending if p not in preview.pending and
                       not any(q["key"] == p["key"] and q.get("item") == p.get("item") for q in preview.pending)]
            if dropped:
                lines.append("\nCancelled pending changes:")
                lines += [f"  • {rules.describe_change(p)}" for p in dropped]
            if not lines:
                messagebox.showinfo("PlayLimiter", "Nothing to change.")
                return
            if not messagebox.askyesno("Save settings?", "\n".join(lines) + "\n\nSave these settings?"):
                return

        store.write_text_atomic(store.config_path(), text)
        note = "" if winsys.daemon_running() else "\n\nNote: PlayLimiter isn't running, so nothing applies until it starts."
        messagebox.showinfo("PlayLimiter", "Saved. PlayLimiter picks the change up within a few seconds." + note)
        self.root.after(3000, self.refresh)

    def start_cooldown(self) -> None:
        state = load_state()
        if state is None:
            return
        cfg = state.active
        if not messagebox.askyesno(
            "Start cooldown now?",
            f"Games will be blocked for {fmt_duration(cfg['cooldown_minutes'] * 60)}, starting now.\n\n"
            f"A running game gets {cfg['grace_seconds']} s to save before it's closed.\n\n"
            "This can't be undone. Start the cooldown?",
        ):
            return
        from .cli import send_request

        ok, message = send_request("start-cooldown")
        (messagebox.showinfo if ok else messagebox.showerror)("PlayLimiter", message)
        self.root.after(200, self.refresh)

    # ------------------------------------------------------------ status

    def refresh(self) -> None:
        state = load_state()
        running = winsys.daemon_running()
        if state is None:
            self.status_var.set("PlayLimiter hasn't run yet." if not running else "Starting…")
        else:
            st = rules.evaluate(state, datetime.now())
            cfg = state.active
            self.cooldown_button.state(["disabled"] if st.reason == "cooldown" else ["!disabled"])
            ends_in = rules.session_ends_in(state)
            if st.reason == "cooldown":
                line = f"Break: play allowed again at {_clock(st.wait_s)} (in {fmt_duration(st.wait_s)})"
            elif st.reason == "daily_cap":
                line = f"Daily cap reached: play allowed again in {fmt_duration(st.wait_s)}"
            elif ends_in is not None:
                line = f"Session ends at {_clock(ends_in)} ({fmt_duration(ends_in)} left)"
            else:
                line = f"Ready: launching a game starts a {fmt_duration(cfg['session_minutes'] * 60)} session"
            today = fmt_duration(state.daily_used_s) if state.daily_used_s else "0 min"
            line += f"   ·   Today: {today}"
            if state.uninstall_remaining_s is not None:
                line += f"\nUninstall scheduled for {_when(state.uninstall_remaining_s)}"
            if not running:
                line += "\n⚠ PlayLimiter is not running. Run 'playlimiter install' to start it."
            self.status_var.set(line)

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
