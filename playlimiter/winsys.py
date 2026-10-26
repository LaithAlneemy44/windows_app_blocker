"""Windows plumbing: uptime clock, single-instance mutex, Task Scheduler."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "PlayLimiter"
MUTEX_NAME = "Local\\PlayLimiter.Daemon"
_CREATE_NO_WINDOW = 0x08000000


def uptime_s() -> float:
    """Seconds since boot, including sleep. Unaffected by wall-clock changes."""
    return ctypes.windll.kernel32.GetTickCount64() / 1000.0


def acquire_single_instance():
    """Return a mutex handle, or None if another daemon already holds it."""
    import win32api
    import win32event
    import winerror

    handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return None
    return handle


def daemon_running() -> bool:
    import pywintypes
    import win32api
    import win32event

    try:
        h = win32event.OpenMutex(win32event.SYNCHRONIZE, False, MUTEX_NAME)
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


def _task_xml(workdir: Path) -> str:
    # The logon trigger is limited to the current user, so no admin rights are needed.
    user = escape(f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>PlayLimiter: limits game playtime.</Description></RegistrationInfo>
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
    <Arguments>-m playlimiter run</Arguments>
    <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
  </Exec></Actions>
</Task>
"""


def register_task(workdir: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
        f.write(_task_xml(workdir))
        xml_path = f.name
    try:
        r = _schtasks("/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F")
    finally:
        os.unlink(xml_path)
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /Create failed: {r.stderr.strip() or r.stdout.strip()}")


def run_task() -> None:
    r = _schtasks("/Run", "/TN", TASK_NAME)
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /Run failed: {r.stderr.strip() or r.stdout.strip()}")


def delete_task() -> bool:
    return _schtasks("/Delete", "/TN", TASK_NAME, "/F").returncode == 0


def task_exists() -> bool:
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0
