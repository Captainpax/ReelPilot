from __future__ import annotations

import win32api
import win32con
import win32gui

from ..domain import WindowBounds


def find_stardew_window() -> int | None:
    matches: list[int] = []

    def inspect_window(window_handle: int, _: object) -> bool:
        if win32gui.IsWindowVisible(window_handle):
            title = win32gui.GetWindowText(window_handle)
            if title.startswith("Stardew Valley"):
                matches.append(window_handle)
                return False
        return True

    win32gui.EnumWindows(inspect_window, None)
    return matches[0] if matches else None


def window_bounds(window_handle: int) -> WindowBounds:
    left, top, right, bottom = win32gui.GetWindowRect(window_handle)
    return WindowBounds(left, top, right, bottom)


def hotkey_pressed_once(virtual_key: int) -> bool:
    return bool(win32api.GetAsyncKeyState(virtual_key) & 1)


def pause_requested() -> bool:
    return hotkey_pressed_once(win32con.VK_F7)


def stop_requested() -> bool:
    return hotkey_pressed_once(win32con.VK_F8)


def force_mouse_up() -> None:
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
