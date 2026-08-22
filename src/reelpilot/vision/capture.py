from __future__ import annotations

from time import perf_counter

import mss
import numpy as np

from ..domain import WindowBounds
from ..platform.windows import window_bounds


class ScreenCapture:
    """Own one persistent MSS capture session for the detected game window."""

    def __init__(self, window_handle: int) -> None:
        self.window_handle = window_handle
        self._capture = mss.mss()
        self._closed = False
        self.last_capture_milliseconds = 0.0

    @property
    def bounds(self) -> WindowBounds:
        return window_bounds(self.window_handle)

    def capture_frame(self) -> np.ndarray:
        return self.capture_bounds(self.bounds)

    def capture_bounds(self, bounds: WindowBounds) -> np.ndarray:
        if self._closed:
            raise RuntimeError("screen capture is closed")
        started = perf_counter()
        shot = self._capture.grab(
            {
                "left": bounds.left_pixels,
                "top": bounds.top_pixels,
                "width": bounds.width_pixels,
                "height": bounds.height_pixels,
            }
        )
        frame = np.asarray(shot)
        self.last_capture_milliseconds = (perf_counter() - started) * 1000.0
        return frame

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._capture.close()

    def __enter__(self) -> ScreenCapture:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
