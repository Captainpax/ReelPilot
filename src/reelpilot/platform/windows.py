"""Small, typed wrappers around the Windows APIs ReelPilot needs."""

from __future__ import annotations

import time

import win32api
import win32con
import win32gui

from ..domain import WindowBounds


def find_stardew_window() -> int | None:
    """Return the first visible top-level Stardew Valley window handle."""
    matches: list[int] = []

    def inspect_window(window_handle: int, _: object) -> bool:
        if win32gui.IsWindowVisible(window_handle):
            title = win32gui.GetWindowText(window_handle)
            if title.startswith("Stardew Valley"):
                matches.append(window_handle)
        # pywin32 reports an early callback stop as a platform error on some
        # Windows builds.  Enumeration is cheap and infrequent, so always let
        # EnumWindows finish and choose the first match afterward.
        return True

    win32gui.EnumWindows(inspect_window, None)
    return matches[0] if matches else None


def window_bounds(window_handle: int) -> WindowBounds:
    """Read absolute screen bounds for ``window_handle``."""
    left, top, right, bottom = win32gui.GetWindowRect(window_handle)
    return WindowBounds(left, top, right, bottom)


def hotkey_pressed_once(virtual_key: int) -> bool:
    """Consume the low-order edge bit for a global virtual key."""
    return bool(win32api.GetAsyncKeyState(virtual_key) & 1)


def stats_requested() -> bool:
    """Return whether F5 requested a historical-statistics refresh."""
    return hotkey_pressed_once(win32con.VK_F5)


def previous_stats_page_requested() -> bool:
    """Return whether Page Up requested the previous species page."""
    return hotkey_pressed_once(win32con.VK_PRIOR)


def next_stats_page_requested() -> bool:
    """Return whether Page Down requested the next species page."""
    return hotkey_pressed_once(win32con.VK_NEXT)


def pause_requested() -> bool:
    """Return whether F7 requested start, pause, or resume."""
    return hotkey_pressed_once(win32con.VK_F7)


def debug_start_requested() -> bool:
    """Return whether F6 requested a diagnostic start."""
    return hotkey_pressed_once(win32con.VK_F6)


def stop_requested() -> bool:
    """Return whether F8 requested emergency shutdown."""
    return hotkey_pressed_once(win32con.VK_F8)


def force_mouse_up() -> None:
    """Release both mouse buttons and common modifiers as a cleanup layer."""
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    for virtual_key in (win32con.VK_SHIFT, win32con.VK_CONTROL, win32con.VK_MENU):
        win32api.keybd_event(virtual_key, 0, win32con.KEYEVENTF_KEYUP, 0)


def prepare_window_capture(window_handle: int) -> None:
    """Focus Stardew and park its cursor outside result/menu source regions."""
    bounds = window_bounds(window_handle)
    win32gui.SetForegroundWindow(window_handle)
    win32api.SetCursorPos(
        (
            bounds.left_pixels + bounds.width_pixels * 3 // 4,
            bounds.top_pixels + bounds.height_pixels * 3 // 4,
        )
    )


def click_window(window_handle: int, x_pixels: int, y_pixels: int) -> None:
    """Left-click a window-relative point and guarantee button-up."""
    _click_window(window_handle, x_pixels, y_pixels, right_button=False)


def right_click_window(window_handle: int, x_pixels: int, y_pixels: int) -> None:
    """Right-click a window-relative point and guarantee button-up."""
    _click_window(window_handle, x_pixels, y_pixels, right_button=True)


def _click_window(
    window_handle: int,
    x_pixels: int,
    y_pixels: int,
    *,
    right_button: bool,
) -> None:
    """Focus, position, and perform one bounded direct mouse click."""
    bounds = window_bounds(window_handle)
    win32gui.SetForegroundWindow(window_handle)
    win32api.SetCursorPos(
        (bounds.left_pixels + int(x_pixels), bounds.top_pixels + int(y_pixels))
    )
    down = (
        win32con.MOUSEEVENTF_RIGHTDOWN
        if right_button
        else win32con.MOUSEEVENTF_LEFTDOWN
    )
    up = (
        win32con.MOUSEEVENTF_RIGHTUP
        if right_button
        else win32con.MOUSEEVENTF_LEFTUP
    )
    # Stardew polls input once per game frame. A down/up pair in the same
    # scheduler slice is occasionally invisible, especially immediately after
    # focus changes. Keep the bounded click active across one polling window.
    time.sleep(0.02)
    win32api.mouse_event(down, 0, 0, 0, 0)
    try:
        time.sleep(0.02)
        win32api.mouse_event(up, 0, 0, 0, 0)
    finally:
        force_mouse_up()


def tap_window_key(window_handle: int, virtual_key: int) -> None:
    """Tap one virtual key in Stardew and guarantee its key-up event."""
    win32gui.SetForegroundWindow(window_handle)
    time.sleep(0.01)
    win32api.keybd_event(virtual_key, 0, 0, 0)
    try:
        time.sleep(0.03)
    finally:
        win32api.keybd_event(virtual_key, 0, win32con.KEYEVENTF_KEYUP, 0)
