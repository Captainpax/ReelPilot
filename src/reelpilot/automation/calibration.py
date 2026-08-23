"""Stable automatic green-bar length calibration."""

from __future__ import annotations

from collections import deque
from statistics import median


class BarCalibrator:
    """Accept a bar length only after five of eight samples agree closely."""

    def __init__(self) -> None:
        """Create an empty eight-sample calibration window."""
        self._samples: deque[int] = deque(maxlen=8)
        self.length_pixels: int | None = None

    def reset(self) -> None:
        """Discard samples and the previously accepted length."""
        self._samples.clear()
        self.length_pixels = None

    def observe(self, length_pixels: int | None) -> int | None:
        """Add one candidate length and return the stable result when available."""
        if length_pixels is None or not 50 <= length_pixels <= 200:
            return self.length_pixels
        self._samples.append(length_pixels)
        groups = [
            [value for value in self._samples if abs(value - candidate) <= 2]
            for candidate in self._samples
        ]
        best = max(groups, key=len)
        if len(best) >= 5:
            self.length_pixels = round(median(best))
        return self.length_pixels
