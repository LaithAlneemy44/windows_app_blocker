"""Toast notifications. Never raises: a failed toast must not stop enforcement."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)
_toaster = None


def toast(title: str, message: str) -> None:
    global _toaster
    log.info("Toast: %s | %s", title, message)
    try:
        from windows_toasts import Toast, WindowsToaster

        if _toaster is None:
            _toaster = WindowsToaster("PlayLimiter")
        _toaster.show_toast(Toast([title, message]))
    except Exception as e:
        log.warning("Toast failed: %s", e)


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds + 59) // 60
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"
