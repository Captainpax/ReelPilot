"""Fail-safe binary-protocol client for the native 20 ms input helper."""

from __future__ import annotations

import struct
import subprocess
import time
from contextlib import suppress
from queue import Empty, Queue
from threading import Thread

from ..platform.paths import input_helper_path
from ..platform.windows import (
    click_window,
    force_mouse_up,
    prepare_window_capture,
    right_click_window,
    tap_window_key,
)


class InputController:
    """Own the native helper process and provide explicit mouse operations."""

    PROTOCOL_VERSION = 1
    OP_IDLE = b"\x00"
    OP_DUTY = b"\x01"
    OP_PRESS = b"\x02"
    OP_RELEASE = b"\x03"
    OP_SHUTDOWN = b"\x04"

    def __init__(self, window_handle: int, *, startup_timeout_seconds: float = 2.0) -> None:
        """Launch the helper for ``window_handle`` and validate its version handshake."""
        helper = input_helper_path()
        if not helper.is_file():
            raise RuntimeError(f"ReelPilot input helper not found: {helper}")
        self._process: subprocess.Popen[bytes] | None = subprocess.Popen(
            [str(helper), str(window_handle)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self._closed = False
        self._window_handle = window_handle
        self._wait_ready(startup_timeout_seconds)

    def __enter__(self) -> InputController:
        """Return this controller for context-managed ownership."""
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Release all input and stop the helper when leaving the context."""
        self.close()

    def set_duty(self, duty_ratio: float) -> None:
        """Send a finite duty ratio clamped to the native protocol range."""
        clamped = max(0.0, min(1.0, float(duty_ratio)))
        self._write(self.OP_DUTY + struct.pack("<f", clamped))

    def press(self) -> None:
        """Send one explicit mouse-down command."""
        self._write(self.OP_PRESS)

    def release(self) -> None:
        """Send mouse-up through both helper and direct Windows fallback."""
        try:
            self._write(self.OP_RELEASE)
        finally:
            force_mouse_up()

    def tap(self, duration_seconds: float = 0.04) -> None:
        """Press for a bounded duration and release even if sleeping fails."""
        self.press()
        try:
            time.sleep(max(0.0, duration_seconds))
        finally:
            self.release()

    def idle(self) -> None:
        """Stop pulse control and force mouse-up through both safety layers."""
        try:
            self._write(self.OP_IDLE)
        finally:
            force_mouse_up()

    def prepare_menu_capture(self) -> None:
        """Release input and move the cursor away from item-menu source slots."""
        self.idle()
        prepare_window_capture(self._window_handle)

    def click_at(self, x_pixels: int, y_pixels: int) -> None:
        """Idle pulse control, then left-click a window-relative point."""
        self.idle()
        click_window(self._window_handle, x_pixels, y_pixels)

    def right_click_at(self, x_pixels: int, y_pixels: int) -> None:
        """Idle pulse control, then right-click a window-relative point."""
        self.idle()
        right_click_window(self._window_handle, x_pixels, y_pixels)

    def tap_key(self, virtual_key: int) -> None:
        """Idle pulse control, then tap a key in the Stardew window."""
        self.idle()
        tap_window_key(self._window_handle, virtual_key)

    def close(self) -> None:
        """Release input and stop, terminate, or finally kill the helper idempotently."""
        if self._closed:
            return
        self._closed = True
        process = self._process
        try:
            force_mouse_up()
            if process is not None and process.poll() is None:
                with suppress(OSError, RuntimeError):
                    self._write_raw(self.OP_RELEASE + self.OP_SHUTDOWN)
                try:
                    process.wait(timeout=0.4)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.5)
        finally:
            force_mouse_up()
            self._process = None

    def _wait_ready(self, timeout_seconds: float) -> None:
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("input helper did not expose a readiness stream")
        stdout = process.stdout
        lines: Queue[bytes] = Queue(maxsize=1)
        reader = Thread(target=lambda: lines.put(stdout.readline()), daemon=True)
        reader.start()
        try:
            line = (
                lines.get(timeout=max(0.01, timeout_seconds)).decode(errors="replace").strip()
            )
        except Empty:
            line = ""
        if line == f"READY {self.PROTOCOL_VERSION}":
            return
        if process.poll() is not None:
            error = process.stderr.read().decode(errors="replace") if process.stderr else ""
            self.close()
            raise RuntimeError(f"input helper exited during startup: {error.strip()}")
        self.close()
        raise RuntimeError("input helper readiness handshake failed")

    def _write(self, payload: bytes) -> None:
        if self._closed:
            raise RuntimeError("input controller is closed")
        self._write_raw(payload)

    def _write_raw(self, payload: bytes) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("input helper is not running")
        process.stdin.write(payload)
        process.stdin.flush()
