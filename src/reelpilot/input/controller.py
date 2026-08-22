from __future__ import annotations

import struct
import subprocess
import time
from contextlib import suppress
from queue import Empty, Queue
from threading import Thread

from ..platform.paths import input_helper_path
from ..platform.windows import force_mouse_up


class InputController:
    PROTOCOL_VERSION = 1
    OP_IDLE = b"\x00"
    OP_DUTY = b"\x01"
    OP_PRESS = b"\x02"
    OP_RELEASE = b"\x03"
    OP_SHUTDOWN = b"\x04"

    def __init__(self, window_handle: int, *, startup_timeout_seconds: float = 2.0) -> None:
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
        self._wait_ready(startup_timeout_seconds)

    def __enter__(self) -> InputController:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def set_duty(self, duty_ratio: float) -> None:
        clamped = max(0.0, min(1.0, float(duty_ratio)))
        self._write(self.OP_DUTY + struct.pack("<f", clamped))

    def press(self) -> None:
        self._write(self.OP_PRESS)

    def release(self) -> None:
        try:
            self._write(self.OP_RELEASE)
        finally:
            force_mouse_up()

    def tap(self, duration_seconds: float = 0.04) -> None:
        self.press()
        try:
            time.sleep(max(0.0, duration_seconds))
        finally:
            self.release()

    def idle(self) -> None:
        try:
            self._write(self.OP_IDLE)
        finally:
            force_mouse_up()

    def close(self) -> None:
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
