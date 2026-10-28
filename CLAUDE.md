# CLAUDE.md

Context for Claude Code working in this repo.

## What this is

Two Windows 10/11 self-control apps in one pip install (`py -m pip install .`):

- **PlayLimiter** (`playlimiter/`): limits playtime for Steam/other games. It has been in use for a while.
  Session model: launching a game starts a session whose clock keeps running even if the game is closed; when it ends the break (`cooldown_minutes`) starts; nothing counts until the next launch. The daily cap counts only time a game is open. A **free-change window** opens at `daily_reset_time` (pending changes apply, and all setting changes apply instantly) and closes at the first game of the day.
- **Browser Curfew** (`curfew/`): installed and running on the Windows PC since 2026-10-28. It closes browsers every night from 22:30 until 07:00. It has a separate logon task (`BrowserCurfew`), data folder (`%LOCALAPPDATA%\BrowserCurfew`), settings window and desktop shortcut ("Browser Curfew", moon icon). It shares no settings or state with PlayLimiter.

The user usually edits on a MacBook and runs the apps on their Windows PC, syncing through GitHub. The README has the user-facing details for both apps.

## Current branch state (as of 2026-10-28)

- Browser Curfew is on **`main`**. It was tested on Windows, then landed by reverting the revert `42748b8` (reapply commit on top of the PlayLimiter changes below), plus this file.
- The **`browser-curfew`** branch is now stale (everything on it is in `main`). It hasn't been deleted; ask before deleting it.
- Ask before force-pushing or deleting branches.

## Shared-code compatibility

Curfew reuses `playlimiter.rules.reconcile()` with its own `State`. PlayLimiter's version must keep working for it:
- `ReconcileResult.tightened` is kept as an alias of `immediate` (the new name, since free-change-window changes can be looser).
- `reconcile()` reads `free_change_window` with `getattr(..., False)`; Curfew's `State` has no such field.
- `tests/test_rules.py::test_reconcile_accepts_a_state_without_free_change_window` guards this.

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

- On Windows: `py -m pip install -e ".[dev]"` then `py -m pytest` (87 tests).
- On the Mac, Windows-only modules don't import. Tests there use a throwaway venv plus a pytest plugin that stubs `winreg`/`win32*`/`winsound` (`-p winstubs`). Don't commit stubs.
- **Live tests on the user's PC must never touch their real games or browsers** (one test once closed a real game). Run a sandbox copy with its own `LOCALAPPDATA` (each data folder gets its own single-instance mutex, so it runs alongside the real apps), set `PLAYLIMITER_IGNORE_STEAM=1` so PlayLimiter skips Steam libraries, and give Curfew `browsers = ["<scratch folder>"]` with a renamed copy of `PING.EXE` as the stand-in. Set `PYTHONPATH` to the repo so the banner subprocess uses the repo code.

## Windows verification (done 2026-10-28, sandboxed)

1. Settings window opens and renders; banner `{clock}` countdown works. ✅ (Real `curfew install` also done: task, daemon and "Browser Curfew" shortcut all fine.)
2. Curfew closes the stand-in browser after the grace; a relaunch during the curfew closes in ~11 s. ✅
3. Warning fires before the curfew. ✅ It used to say "15 minutes" even when the curfew had just been moved to 2 minutes away; it now shows the real time left (same fix applied to PlayLimiter's warning).
4. Later change: first allowed, second refused; earlier still allowed. ✅
5. PlayLimiter unaffected (all tests pass, shared banner/notify/winsys fine). ✅

Not yet seen with a real Chrome/Edge window (tab-restore prompt, banner over the tab strip).

Known caveats:
- The banner covers the top of the browser (tabs and address bar) for 5 min. It's click-through, but the user may want it moved or made smaller.
- Background `msedge.exe` (startup boost) also gets closed during the curfew. The "blocked" notice is rate-limited to once a minute.
- Changing the system clock gets around the curfew. This is friction, not security, same as PlayLimiter.

Logs: `%LOCALAPPDATA%\BrowserCurfew\curfew.log`.
