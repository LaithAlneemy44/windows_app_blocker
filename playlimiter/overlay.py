"""On-screen banner shown over games.

Toasts are hidden while a full-screen game is running, so warnings also show this:
a topmost, click-through popup that never takes focus, so the game keeps its input.
It is a raw Win32 window (no Tk: Tk activates its windows when they open).
Runs in its own process (``python -m playlimiter overlay ...``) so the daemon loop never blocks.
"""
from __future__ import annotations

import ctypes
import subprocess
import time

import win32api
import win32con
import win32gui

from . import winsys

BG = win32api.RGB(17, 24, 39)
ACCENT = win32api.RGB(245, 158, 11)
FG = win32api.RGB(249, 250, 251)
ALPHA = 250
TIMER_ID = 1
_EX_STYLE = (
    win32con.WS_EX_TOPMOST
    | win32con.WS_EX_TOOLWINDOW  # no taskbar button
    | win32con.WS_EX_NOACTIVATE  # never steals focus from the game
    | win32con.WS_EX_TRANSPARENT  # clicks go through to the game
    | win32con.WS_EX_LAYERED
)


def show_async(title: str, message: str, seconds: int) -> None:
    """Start the banner in a separate hidden process. ``{left}`` in message counts down."""
    try:
        subprocess.Popen(
            [winsys.pythonw(), "-m", "playlimiter", "overlay", "--seconds", str(seconds), title, message],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            close_fds=True,
        )
    except OSError:
        pass


def _font(points: int, dpi: int, bold: bool):
    lf = win32gui.LOGFONT()
    lf.lfHeight = -round(points * dpi / 72)
    lf.lfWeight = win32con.FW_BOLD if bold else win32con.FW_NORMAL
    lf.lfFaceName = "Segoe UI"
    lf.lfQuality = 5  # CLEARTYPE_QUALITY
    return win32gui.CreateFontIndirect(lf)


def show(title: str, message: str, seconds: int) -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass

    # Put it on the monitor showing the foreground window (where the game is).
    monitor = win32api.MonitorFromWindow(win32gui.GetForegroundWindow(), win32con.MONITOR_DEFAULTTOPRIMARY)
    mon_left, mon_top, mon_right, _ = win32api.GetMonitorInfo(monitor)["Monitor"]
    dpi_x, dpi_y = ctypes.c_uint(96), ctypes.c_uint(96)
    try:
        ctypes.windll.shcore.GetDpiForMonitor(int(monitor), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
    except (AttributeError, OSError):
        pass
    dpi = dpi_y.value
    px = lambda v: round(v * dpi / 96)

    title_font, body_font = _font(20, dpi, True), _font(15, dpi, False)
    width = min(px(820), mon_right - mon_left - px(40))
    pad, bar = px(20), px(8)
    text_w = width - bar - 2 * pad

    deadline = time.monotonic() + seconds
    state = {"text": message.replace("{left}", str(seconds))}

    def measure(font, text):
        hdc = win32gui.GetDC(0)
        old = win32gui.SelectObject(hdc, font)
        height, _ = win32gui.DrawText(hdc, text, -1, (0, 0, text_w, 0), win32con.DT_CALCRECT | win32con.DT_WORDBREAK)
        win32gui.SelectObject(hdc, old)
        win32gui.ReleaseDC(0, hdc)
        return height

    title_h = measure(title_font, title)
    height = pad + title_h + px(4) + measure(body_font, state["text"]) + pad
    x = mon_left + (mon_right - mon_left - width) // 2
    y = mon_top + px(40)

    def on_paint(hwnd):
        hdc, ps = win32gui.BeginPaint(hwnd)
        win32gui.FillRect(hdc, (0, 0, width, height), win32gui.CreateSolidBrush(BG))
        win32gui.FillRect(hdc, (0, 0, bar, height), win32gui.CreateSolidBrush(ACCENT))
        win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
        left = bar + pad
        win32gui.SelectObject(hdc, title_font)
        win32gui.SetTextColor(hdc, ACCENT)
        win32gui.DrawText(hdc, title, -1, (left, pad, left + text_w, pad + title_h), win32con.DT_WORDBREAK)
        win32gui.SelectObject(hdc, body_font)
        win32gui.SetTextColor(hdc, FG)
        top = pad + title_h + px(4)
        win32gui.DrawText(hdc, state["text"], -1, (left, top, left + text_w, height), win32con.DT_WORDBREAK)
        win32gui.EndPaint(hwnd, ps)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == win32con.WM_PAINT:
            on_paint(hwnd)
            return 0
        if msg == win32con.WM_TIMER:
            left_s = max(0, round(deadline - time.monotonic()))
            if left_s <= 0:
                win32gui.DestroyWindow(hwnd)
                return 0
            state["text"] = message.replace("{left}", str(left_s))
            win32gui.InvalidateRect(hwnd, None, True)
            # Games sometimes put themselves on top; keep re-asserting without activating.
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
            return 0
        if msg == win32con.WM_MOUSEACTIVATE:
            return win32con.MA_NOACTIVATE
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    wc = win32gui.WNDCLASS()
    wc.lpszClassName = "PlayLimiterBanner"
    wc.lpfnWndProc = wndproc
    wc.hInstance = win32api.GetModuleHandle(None)
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindowEx(
        _EX_STYLE, wc.lpszClassName, "PlayLimiter", win32con.WS_POPUP,
        x, y, width, height, 0, 0, wc.hInstance, None,
    )
    win32gui.SetLayeredWindowAttributes(hwnd, 0, ALPHA, win32con.LWA_ALPHA)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    ctypes.windll.user32.SetTimer(hwnd, TIMER_ID, 500, None)
    win32gui.PumpMessages()
