"""Locate Steam and its library folders."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict:
    """Minimal Valve KeyValues parser (quoted keys/values and braces)."""
    root: dict = {}
    stack = [root]
    key = None
    for m in _TOKEN.finditer(text):
        s, brace = m.group(1), m.group(2)
        if brace == "{":
            child: dict = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
        elif key is None:
            key = s.replace("\\\\", "\\")
        else:
            stack[-1][key] = s.replace("\\\\", "\\")
            key = None
    return root


def library_paths_from_vdf(text: str) -> list[str]:
    data = parse_vdf(text)
    folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
    paths = []
    for value in folders.values():
        if isinstance(value, dict) and "path" in value:
            paths.append(value["path"])
        elif isinstance(value, str) and ("\\" in value or "/" in value):
            paths.append(value)  # very old format: "1" "D:\\SteamLibrary"
    return paths


def find_steam_dir() -> Path | None:
    candidates = []
    try:
        import winreg

        for hive, sub, name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    candidates.append(winreg.QueryValueEx(k, name)[0])
            except OSError:
                pass
    except ImportError:
        pass
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates.append(os.path.join(pf86, "Steam"))
    for c in candidates:
        p = Path(c)
        if p.is_dir():
            return p
    return None


def steam_common_dirs() -> list[Path]:
    """Every <library>\\steamapps\\common folder that exists."""
    if os.environ.get("PLAYLIMITER_IGNORE_STEAM"):
        return []  # for test runs, so they never touch real games
    steam = find_steam_dir()
    if steam is None:
        log.warning("Steam install not found")
        return []
    libraries = [str(steam)]
    for vdf in (steam / "steamapps" / "libraryfolders.vdf", steam / "config" / "libraryfolders.vdf"):
        try:
            libraries += library_paths_from_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    seen, out = set(), []
    for lib in libraries:
        common = Path(lib) / "steamapps" / "common"
        key = os.path.normcase(str(common))
        if key not in seen and common.is_dir():
            seen.add(key)
            out.append(common)
    return out
