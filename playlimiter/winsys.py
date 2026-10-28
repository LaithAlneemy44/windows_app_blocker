"""Windows plumbing: uptime clock, single-instance mutex, Task Scheduler."""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "PlayLimiter"
_CREATE_NO_WINDOW = 0x08000000


def mutex_name(app: str, data: Path) -> str:
    # One daemon per data folder (lets a test copy with its own LOCALAPPDATA run alongside).
    key = hashlib.sha1(str(data).casefold().encode()).hexdigest()[:12]
    return f"Local\\{app}.Daemon.{key}"


def _mutex_name() -> str:
    from .store import data_dir

    return mutex_name("PlayLimiter", data_dir())


def uptime_s() -> float:
    """Seconds since boot, including sleep. Unaffected by wall-clock changes."""
    return ctypes.windll.kernel32.GetTickCount64() / 1000.0


def acquire_single_instance(name: str | None = None):
    """Return a mutex handle, or None if another daemon already holds it."""
    import win32api
    import win32event
    import winerror

    handle = win32event.CreateMutex(None, False, name or _mutex_name())
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return None
    return handle


def daemon_running(name: str | None = None) -> bool:
    import pywintypes
    import win32api
    import win32event

    try:
        h = win32event.OpenMutex(win32event.SYNCHRONIZE, False, name or _mutex_name())
    except pywintypes.error:
        return False
    win32api.CloseHandle(h)
    return True


def pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW
    )


def _task_xml(workdir: Path, module: str, description: str) -> str:
    # The logon trigger is limited to the current user, so no admin rights are needed.
    user = escape(f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{escape(pythonw())}</Command>
    <Arguments>-m {module} run</Arguments>
    <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
  </Exec></Actions>
</Task>
"""


def register_task(workdir: Path, name: str = TASK_NAME, module: str = "playlimiter",
                  description: str = "PlayLimiter: limits game playtime.") -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
        f.write(_task_xml(workdir, module, description))
        xml_path = f.name
    try:
        r = _schtasks("/Create", "/TN", name, "/XML", xml_path, "/F")
    finally:
        os.unlink(xml_path)
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /Create failed: {r.stderr.strip() or r.stdout.strip()}")


def run_task(name: str = TASK_NAME) -> None:
    r = _schtasks("/Run", "/TN", name)
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /Run failed: {r.stderr.strip() or r.stdout.strip()}")


def delete_task(name: str = TASK_NAME) -> bool:
    return _schtasks("/Delete", "/TN", name, "/F").returncode == 0


def task_exists(name: str = TASK_NAME) -> bool:
    return _schtasks("/Query", "/TN", name).returncode == 0


SHORTCUT_NAME = "PlayLimiter Settings.lnk"
ICON_PATH = Path(__file__).with_name("icon.ico")


def desktop_dir() -> Path:
    # Ask the shell, so a Desktop redirected to OneDrive is handled.
    from win32com.shell import shell, shellcon

    return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0))


def shortcut_path() -> Path:
    return desktop_dir() / SHORTCUT_NAME


def make_shortcut(path: Path, module: str, icon: Path, description: str) -> Path:
    """Shortcut that opens ``module``'s settings window without a console."""
    import win32com.client

    link = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(path))
    link.TargetPath = pythonw()
    link.Arguments = f"-m {module} settings"
    link.WorkingDirectory = str(Path.home())
    link.IconLocation = str(icon)
    link.Description = description
    link.Save()
    return path


def create_shortcut() -> Path:
    return make_shortcut(shortcut_path(), "playlimiter", ICON_PATH, "Change PlayLimiter settings")


def remove_shortcut(path: Path | None = None) -> None:
    try:
        (path or shortcut_path()).unlink(missing_ok=True)
    except OSError:
        pass
