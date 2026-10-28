# PlayLimiter

A small Windows 10/11 background app that limits playtime for Steam games (and any other folders you add). It is a self-control tool: it adds friction against impulsive "just one more hour" overrides. It is not parental-control software and makes no attempt to be tamper-proof.

## Disclaimer

- **PlayLimiter closes running games.** It asks each game to close first, then force-closes it after a grace period. Either can cause **lost or corrupted save data**, so save when you get a warning.
- The software is provided **as is, without warranty** of any kind (see [LICENSE](LICENSE)). Use it at your own risk.
- PlayLimiter is **not affiliated with or endorsed by Valve or Steam**. "Steam" is used only to describe which games it can detect.

## How it works

- Every 2 seconds it lists running processes. Any program under a Steam library's `steamapps\common` folder, or under one of your `extra_paths`, counts as a game. Steam libraries are found from the registry and `libraryfolders.vdf`.
- **Session budget** (`session_minutes`): play time is counted only while a game is running. When the budget runs out, games are blocked for `cooldown_minutes`.
- **Refills only after a real break.** The budget refills only after a continuous no-play gap of `cooldown_minutes`. Quitting at 89 of 90 minutes and relaunching does not reset it.
- **Optional daily cap** (`daily_cap_minutes`, 0 = off), resetting at `daily_reset_time`.
- **Warnings**: at `warning_minutes` before the limit, you get a sound, a toast notification, and a banner across the top of the screen. Windows hides toasts during full-screen games, but the banner still shows. It stays above the game, can't be clicked, and never takes focus. When time runs out, the banner counts down the grace period.
- **Closing games**: at the limit, PlayLimiter posts `WM_CLOSE` to every top-level window of each game process. It force-terminates a game only if it is still running after `grace_seconds`. A game launched while blocked gets the same treatment, plus a toast saying when play is allowed again.
- **Delayed loosening**: stricter settings apply immediately. Looser ones, such as a longer session, shorter cooldown, higher or disabled cap, new exclusions or longer grace, wait `loosen_delay_hours` (default 24). Uninstalling waits the same amount of time.
- **Clock-safe**: durations are measured with the system uptime counter, not the wall clock. Setting the clock back or forward does not shorten a cooldown, refill the budget, or speed up pending changes. State survives restarts and reboots.

## Install

Requirements: Windows 10/11 and Python 3.11+ from [python.org](https://www.python.org/downloads/) (it includes `pythonw.exe`). No admin rights are needed.

```powershell
git clone https://github.com/LaithAlneemy44/windows_app_blocker.git
cd windows_app_blocker
py -m pip install .
playlimiter install
```

`playlimiter install`:

1. creates `%LOCALAPPDATA%\PlayLimiter\config.toml` from [config.example.toml](playlimiter/config.example.toml) if it doesn't exist yet,
2. registers a Task Scheduler task, **PlayLimiter**, that starts the app hidden (via `pythonw`) whenever you log on,
3. starts the app right away, and
4. puts a **PlayLimiter Settings** shortcut on your desktop.

If `playlimiter` isn't found, the Python `Scripts` folder isn't on your PATH. Use `py -m playlimiter install` instead (same for all commands below).

Install with plain `pip install .`, not `pip install -e .`, so the app doesn't depend on the clone folder. If you reinstall or upgrade Python, run `playlimiter install` again.

## Configuration

The easiest way is the **PlayLimiter Settings** desktop shortcut (or `playlimiter settings`). It shows:

- your remaining play time and today's total,
- each setting's active value, and any pending change with the date it applies.

Before saving, it lists which changes apply immediately and which have to wait.

You can also edit `%LOCALAPPDATA%\PlayLimiter\config.toml` directly. Changes are picked up within a couple of seconds. If the file is invalid, the current settings are kept, and the error is logged and shown as a toast.

```toml
session_minutes = 90        # max continuous play
cooldown_minutes = 180      # block length after the budget runs out; also the refill gap
daily_cap_minutes = 0       # 0 = off
daily_reset_time = "04:00"
warning_minutes = [10, 2]
grace_seconds = 60          # wait after asking games to close before force-closing
loosen_delay_hours = 24
exclude = []                # e.g. ["wallpaper32.exe", "wallpaper64.exe"] or full paths/folders
extra_paths = []            # folders of non-Steam games, e.g. ["D:\\Games\\Other"]
```

| Change | Effect |
|---|---|
| Shorter session, longer cooldown, lower cap, longer delay, removing an `exclude` entry, adding an `extra_paths` entry | Applies immediately |
| Longer session, shorter cooldown, higher or disabled cap, longer grace, shorter delay, changing reset time, adding an `exclude` entry, removing an `extra_paths` entry | Pending for `loosen_delay_hours`, with a toast showing when it applies |
| `warning_minutes` | Applies immediately |

Notes:

- Lowering `loosen_delay_hours` is itself a loosening, so it can't be used to speed itself up.
- Reverting a pending change cancels it.
- Steam's redistributable installers (`steamapps\common\Steamworks Shared`) are never counted.

## Commands

| Command | What it does |
|---|---|
| `playlimiter status` | Budget left, cooldown remaining, today's usage, active settings, pending changes and when they apply |
| `playlimiter settings` | Open the settings window |
| `playlimiter install` | Create the config if missing, register the logon task, start the app, add the desktop shortcut |
| `playlimiter shortcut` | Recreate the desktop shortcut |
| `playlimiter start-cooldown` | Start the cooldown now, even with play time left. It's stricter, so it applies immediately and can't be undone. Also available as a button in the settings window. |
| `playlimiter uninstall` | Schedule removal after `loosen_delay_hours` |
| `playlimiter cancel-uninstall` | Cancel a scheduled removal immediately |

When a scheduled uninstall comes due, the app deletes its logon task, its desktop shortcut and `%LOCALAPPDATA%\PlayLimiter`, then exits. To remove the program itself afterwards, run `py -m pip uninstall playlimiter`.

Files, all in `%LOCALAPPDATA%\PlayLimiter`:

- `config.toml`: your settings
- `state.json`: active settings, budget, cooldown, pending changes. Written atomically.
- `playlimiter.log`

## Browser Curfew

A second, separate app in the same install. It closes web browsers at night: from **22:30** until **07:00** by default. It has its own background task, its own settings window with its own desktop shortcut (**Browser Curfew**, a moon icon), and its own data folder. It doesn't share any settings or state with PlayLimiter.

```powershell
curfew install
```

This works like `playlimiter install`: it creates `%LOCALAPPDATA%\BrowserCurfew\config.toml`, registers a **BrowserCurfew** logon task, starts the app and adds the **Browser Curfew** desktop shortcut. Use `py -m curfew install` if `curfew` isn't on your PATH.

**At the curfew**, every open browser gets a banner across the top of the screen. It tells you to save anything you have open and counts down **5 minutes** (`grace_minutes`). After the countdown, browsers are force-closed. They're asked to close normally first, so Chrome and Edge can restore your tabs. A browser opened during the curfew is closed after 10 seconds. You get a warning 15 minutes before the curfew (`warning_minutes`), but only if a browser is open.

**Changing just tonight** (the "Tonight only" box in the settings window, or `curfew tonight 23:30`):

- **Later**: once per day, and only before **16:00** (`change_deadline`). Plan it ahead, e.g. for an assignment due that night. You can't make it later once the curfew has started.
- **Earlier**: allowed any time, and it doesn't use up the day's change.
- Either way it only covers that night. The next day is back to the default.

A "day" runs from 07:00 to 07:00, so a change to 01:00 counts as tonight.

**Changing the defaults** (the "Every night" part of the window, or `config.toml`) follows PlayLimiter's rules. Stricter changes apply right away: an earlier curfew, an earlier change deadline, a shorter save time, or adding a browser. Looser changes wait `loosen_delay_hours` (24 h), and any change to `wake_time` counts as looser.

```toml
bedtime = "22:30"          # browsers close at this time every night
wake_time = "07:00"        # and are allowed again from this time
change_deadline = "16:00"  # tonight can only be made later before this
warning_minutes = [15]
grace_minutes = 5          # banner countdown before force-closing
loosen_delay_hours = 24
browsers = ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", ...]  # exe names or full paths
```

| Command | What it does |
|---|---|
| `curfew status` | Tonight's curfew time, whether today's change is used, active settings, pending changes |
| `curfew settings` | Open the settings window |
| `curfew tonight HH:MM` | Change tonight's curfew time (rules above) |
| `curfew install` / `curfew shortcut` | Same as the PlayLimiter versions |
| `curfew uninstall` / `curfew cancel-uninstall` | Schedule or cancel removal after `loosen_delay_hours` |

Files are in `%LOCALAPPDATA%\BrowserCurfew`: `config.toml`, `state.json`, `curfew.log`.

Limitations: the curfew follows the Windows clock, so changing the clock gets around it. Only programs in `browsers` are closed. Apps with a built-in browser (Edge WebView2, Electron apps) aren't affected. The same "friction, not security" caveats as PlayLimiter apply.

## Anti-cheat note

PlayLimiter only reads the process list, reads Steam's `libraryfolders.vdf`, and sends standard window close messages (`WM_CLOSE`). If a game ignores those, it terminates the process with the normal Windows API. It does not inject into, read the memory of, or modify any game. Still, if you play games with kernel-level anti-cheat, test with care.

## Limitations (by design)

This is friction, not security. Anyone with access to the account can end the process in Task Manager, delete the scheduled task, or edit `state.json`. Some more specific behaviour:

- **Restarting the app.** If the app is restarted without a reboot, the downtime is not counted as a break, since games could have run unchecked.
- **Reboots.** After a reboot, the gap is credited as no-play time based on the wall clock, and never as negative time.
- **Sleep.** If the PC sleeps with a game open, the sleep counts as a break, not as play.

## Development

```powershell
py -m pip install -e ".[dev]"
py -m pytest
```

The rules (sessions, cooldowns, daily cap, delayed loosening, uninstall timer) are in `playlimiter/rules.py`, with no Windows or file I/O dependencies. Tests drive them with a fake clock. Windows-specific code is in `detect.py`, `enforce.py`, `notify.py`, `steam.py` and `winsys.py`. The settings window is `gui.py` (tkinter).

Browser Curfew lives in `curfew/`. Its rules are in `curfew/rules.py`, also pure and tested with fixed times (`tests/test_curfew.py`). It reuses PlayLimiter's closing, banner, toast and Task Scheduler code.

## Support

This is a personal project, offered with no guarantee of maintenance. Issues and pull requests may or may not get a response.

## License

[MIT](LICENSE)
