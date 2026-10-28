# CLAUDE.md

Context for Claude Code working in this repo.

## What this is

Two Windows 10/11 self-control apps in one pip install (`py -m pip install .`):

- **PlayLimiter** (`playlimiter/`): limits playtime for Steam/other games. It has been in use for a while.
- **Browser Curfew** (`curfew/`): new, and **not yet tested on Windows**. It closes browsers every night from 22:30 until 07:00. It has a separate logon task (`BrowserCurfew`), data folder (`%LOCALAPPDATA%\BrowserCurfew`), settings window and desktop shortcut ("Browser Curfew", moon icon). It shares no settings or state with PlayLimiter.

The user usually edits on a MacBook and runs the apps on their Windows PC, syncing through GitHub. The README has the user-facing details for both apps.

## Current branch state (as of 2026-10-28)

- Browser Curfew is on the **`browser-curfew`** branch (commit `dd17a7f`), waiting to be tested on Windows.
- `main` briefly had that commit. It was undone with a **revert commit (`42748b8`)**, so `main`'s files match the previous commit, `53fc9b8`.
- Because `dd17a7f` is already in `main`'s history, **merging `browser-curfew` into `main` does nothing**. Once the user confirms it works, put it on `main` by reverting the revert: `git checkout main && git pull && git revert 42748b8 && git push`. Then also merge any later fixes from `browser-curfew`, such as this file.
- Ask before force-pushing or deleting branches.

## Browser Curfew rules (what the user asked for)

- Browsers close at `bedtime` (default **22:30**) and are allowed again at `wake_time` (default **07:00**; the user asked for 7am). A "curfew day" runs from wake_time to wake_time, so 01:00 belongs to the evening before it.
- At the curfew, a topmost, click-through banner says to save your work and counts down **5 minutes** (`grace_minutes`; PlayLimiter's equivalent is 60 s). Browsers are then closed: `WM_CLOSE` first, force-kill after the grace. A browser launched during the curfew is closed after 10 s. There's a warning 15 min before, but only if a browser is open.
- **Tonight only** (settings window's top box or `curfew tonight HH:MM`): making it *later* is allowed **once per day, before 16:00** (`change_deadline`), and not once the curfew has started. Making it *earlier* is allowed any time and doesn't use up the day's change. Either way, the next day goes back to the default. Use case: planned in advance for an assignment due that night.
- Default settings (config.toml or the window's "Every night" section) follow PlayLimiter's rules. Stricter changes apply immediately. Looser ones wait `loosen_delay_hours` (24 h), and any `wake_time` change counts as looser.
- Only each browser's main process is targeted (its parent isn't a browser). Chrome-style helper processes exit with it, and orphaned helpers are treated as main processes.

## Code layout

- `curfew/rules.py`: pure logic (no Windows, files or real clock), tested with fixed datetimes in `tests/test_curfew.py`. It reuses `playlimiter.rules` helpers (`day_key`, `downtime_credit`, `_count_down_timers`, `reconcile`, `request_uninstall`).
- `curfew/daemon.py`, `cli.py`, `gui.py`, `config.py`, `store.py`, `detect.py`, `winsys.py` mirror the matching PlayLimiter modules. Follow their style.
- Shared PlayLimiter code was generalised for curfew in backwards-compatible ways. The defaults keep PlayLimiter's behaviour:
  - `rules.reconcile(..., looser=, list_loose_op=)`
  - `notify.set_app_name()`
  - overlay `{clock}` (m:ss) placeholder
  - `winsys.mutex_name()`, `make_shortcut()`, and name parameters on the task, mutex and shortcut helpers
- The banner always runs as `python -m playlimiter overlay ...`, for both apps.

## Testing

- On Windows: `py -m pip install -e ".[dev]"` then `py -m pytest` (77 tests).
- On the Mac, Windows-only modules don't import. Tests there use a throwaway venv plus a pytest plugin that stubs `winreg`/`win32*`/`winsound` (`-p winstubs`). Don't commit stubs.

## Still to verify on Windows (never run there yet)

1. `curfew install` registers the task, starts the daemon and creates the desktop shortcut with the moon icon. `curfew status` and the settings window open.
2. To test the curfew quickly: set tonight a few minutes earlier than now in the settings window (earlier is always allowed). The banner should count down 5:00 over the browser, then Chrome/Edge should close and offer to restore tabs. Reopening during the curfew should close within ~10 s.
3. The warning toast and banner appear 15 min before (set `warning_minutes` or tonight's time to test).
4. Later-change rules: refused after 16:00 or on a second attempt the same day.
5. PlayLimiter still works as before (shortcut, banner, cooldown).

Known caveats:
- The banner covers the top of the browser (tabs and address bar) for 5 min. It's click-through, but the user may want it moved or made smaller.
- Background `msedge.exe` (startup boost) also gets closed during the curfew. The "blocked" notice is rate-limited to once a minute.
- Changing the system clock gets around the curfew. This is friction, not security, same as PlayLimiter.

Logs: `%LOCALAPPDATA%\BrowserCurfew\curfew.log`.
