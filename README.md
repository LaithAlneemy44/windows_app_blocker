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
- **Warnings**: toast notifications at `warning_minutes` before the limit.
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
| `playlimiter uninstall` | Schedule removal after `loosen_delay_hours` |
| `playlimiter cancel-uninstall` | Cancel a scheduled removal immediately |

When a scheduled uninstall comes due, the app deletes its logon task, its desktop shortcut and `%LOCALAPPDATA%\PlayLimiter`, then exits. To remove the program itself afterwards, run `py -m pip uninstall playlimiter`.

Files, all in `%LOCALAPPDATA%\PlayLimiter`:

- `config.toml`: your settings
- `state.json`: active settings, budget, cooldown, pending changes. Written atomically.
- `playlimiter.log`

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

## Support

This is a personal project, offered with no guarantee of maintenance. Issues and pull requests may or may not get a response.

## License

[MIT](LICENSE)
