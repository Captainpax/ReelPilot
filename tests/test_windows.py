"""Regression tests for the small Windows platform boundary."""

from __future__ import annotations

from reelpilot.platform import windows


def test_window_enumeration_does_not_stop_callback_early(monkeypatch) -> None:
    """Finding Stardew must not abort ``EnumWindows`` through its callback."""
    callback_results: list[bool] = []

    def enumerate_windows(callback, argument) -> None:
        callback_results.append(callback(101, argument))
        callback_results.append(callback(202, argument))

    monkeypatch.setattr(windows.win32gui, "EnumWindows", enumerate_windows)
    monkeypatch.setattr(windows.win32gui, "IsWindowVisible", lambda _handle: True)
    monkeypatch.setattr(
        windows.win32gui,
        "GetWindowText",
        lambda handle: "Stardew Valley" if handle == 101 else "Other",
    )

    assert windows.find_stardew_window() == 101
    assert callback_results == [True, True]


def test_force_mouse_up_releases_both_buttons_and_modifiers(monkeypatch) -> None:
    mouse_events: list[int] = []
    key_events: list[tuple[int, int]] = []
    monkeypatch.setattr(
        windows.win32api,
        "mouse_event",
        lambda flags, _x, _y, _data, _extra: mouse_events.append(flags),
    )
    monkeypatch.setattr(
        windows.win32api,
        "keybd_event",
        lambda key, _scan, flags, _extra: key_events.append((key, flags)),
    )

    windows.force_mouse_up()

    assert mouse_events == [
        windows.win32con.MOUSEEVENTF_LEFTUP,
        windows.win32con.MOUSEEVENTF_RIGHTUP,
    ]
    assert {key for key, _flags in key_events} == {
        windows.win32con.VK_SHIFT,
        windows.win32con.VK_CONTROL,
        windows.win32con.VK_MENU,
    }
    assert all(flags == windows.win32con.KEYEVENTF_KEYUP for _key, flags in key_events)


def test_right_click_uses_window_relative_coordinates_and_always_releases(monkeypatch) -> None:
    cursor_positions: list[tuple[int, int]] = []
    mouse_events: list[int] = []
    monkeypatch.setattr(windows.win32gui, "GetWindowRect", lambda _handle: (100, 50, 900, 650))
    monkeypatch.setattr(windows.win32gui, "SetForegroundWindow", lambda _handle: None)
    monkeypatch.setattr(windows.win32api, "SetCursorPos", cursor_positions.append)
    monkeypatch.setattr(
        windows.win32api,
        "mouse_event",
        lambda flags, _x, _y, _data, _extra: mouse_events.append(flags),
    )
    monkeypatch.setattr(windows.win32api, "keybd_event", lambda *_args: None)
    monkeypatch.setattr(windows.time, "sleep", lambda _seconds: None)

    windows.right_click_window(123, 20, 30)

    assert cursor_positions == [(120, 80)]
    assert mouse_events[:2] == [
        windows.win32con.MOUSEEVENTF_RIGHTDOWN,
        windows.win32con.MOUSEEVENTF_RIGHTUP,
    ]
    assert mouse_events[-2:] == [
        windows.win32con.MOUSEEVENTF_LEFTUP,
        windows.win32con.MOUSEEVENTF_RIGHTUP,
    ]


def test_prepare_window_capture_parks_cursor_at_supported_playfield_point(
    monkeypatch,
) -> None:
    cursor_positions: list[tuple[int, int]] = []
    monkeypatch.setattr(
        windows.win32gui,
        "GetWindowRect",
        lambda _handle: (100, 50, 900, 650),
    )
    monkeypatch.setattr(windows.win32gui, "SetForegroundWindow", lambda _handle: None)
    monkeypatch.setattr(windows.win32api, "SetCursorPos", cursor_positions.append)

    windows.prepare_window_capture(123)

    assert cursor_positions == [(700, 500)]


def test_tap_window_key_spans_a_game_polling_window(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(windows.win32gui, "SetForegroundWindow", lambda _handle: None)
    monkeypatch.setattr(
        windows.win32api,
        "keybd_event",
        lambda key, _scan, flags, _extra: events.append(f"key:{key}:{flags}"),
    )
    monkeypatch.setattr(
        windows.time,
        "sleep",
        lambda seconds: events.append(f"sleep:{seconds:.2f}"),
    )

    windows.tap_window_key(123, 0x1B)

    assert events == [
        "sleep:0.01",
        "key:27:0",
        "sleep:0.03",
        f"key:27:{windows.win32con.KEYEVENTF_KEYUP}",
    ]
