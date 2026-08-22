from __future__ import annotations

from time import perf_counter

import numpy as np

from ..domain import CastObservation, CatchObservation, FishingObservation, WindowBounds
from ..platform.windows import find_stardew_window
from .bite import BiteDetector
from .capture import ScreenCapture
from .cast_meter import CastMeterDetector
from .catch_card import CatchCardDetector, CatchResultReader
from .fishing_ui import FishingUiDetector


class VisionPipeline:
    """Capture once and route frames through independently testable detectors."""

    def __init__(self, window_handle: int | None = None) -> None:
        self.window_handle = (
            window_handle if window_handle is not None else find_stardew_window()
        )
        if self.window_handle is None:
            raise RuntimeError("Stardew Valley window not found")
        self.capture = ScreenCapture(self.window_handle)
        self.fishing_ui = FishingUiDetector()
        self.cast_meter = CastMeterDetector()
        self.bite = BiteDetector()
        self.catch_card = CatchCardDetector()
        self.catch_reader = CatchResultReader()
        self._last_full_frame: np.ndarray | None = None
        self._last_catch_card: np.ndarray | None = None
        self._closed = False

    @property
    def game_bounds(self) -> WindowBounds:
        return self.capture.bounds

    def observe_scene(
        self,
        expected_bar_length_pixels: int | None = None,
    ) -> FishingObservation:
        started = perf_counter()
        bounds = self.fishing_ui.last_bounds
        if bounds is not None:
            game = self.game_bounds
            absolute = WindowBounds(
                game.left_pixels + bounds.left_pixels,
                game.top_pixels + bounds.top_pixels,
                game.left_pixels + bounds.right_pixels,
                game.top_pixels + bounds.bottom_pixels,
            )
            crop = self.capture.capture_bounds(absolute)
            self._last_full_frame = crop
            if self.fishing_ui.is_valid_crop(crop):
                result = self.fishing_ui.analyze(crop, expected_bar_length_pixels)
                return self._with_timings(result, started)
            self.fishing_ui.reset()

        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        located = self.fishing_ui.locate(frame)
        if located is None:
            return self._with_timings(FishingObservation(False), started)
        crop = self.fishing_ui.crop(frame, located)
        return self._with_timings(
            self.fishing_ui.analyze(crop, expected_bar_length_pixels), started
        )

    def begin_cast(self) -> None:
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        self.cast_meter.begin(frame)

    def observe_cast(self) -> CastObservation:
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        return self.cast_meter.observe(frame)

    def detect_bite(self) -> bool:
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        detected, _ = self.bite.detect(frame)
        return detected

    def read_catch(self) -> CatchObservation:
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        bounds = self.catch_card.locate(frame)
        if bounds is None:
            return CatchObservation(False)
        left, top, right, bottom = bounds
        card = frame[top:bottom, left:right, :3].copy()
        self._last_catch_card = card
        return self.catch_reader.read(card, bounds)

    def latest_catch_card(self) -> np.ndarray | None:
        return self._last_catch_card

    def latest_frame(self) -> np.ndarray | None:
        return self._last_full_frame

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.capture.close()

    def _with_timings(
        self,
        observation: FishingObservation,
        started_seconds: float,
    ) -> FishingObservation:
        total_milliseconds = (perf_counter() - started_seconds) * 1000.0
        detection_milliseconds = max(
            0.0, total_milliseconds - self.capture.last_capture_milliseconds
        )
        return FishingObservation(
            observation.ui_detected,
            observation.fish_center_y_pixels,
            observation.bar_center_y_pixels,
            observation.bar_length_pixels,
            observation.progress_ratio,
            observation.fish_confidence,
            observation.bar_confidence,
            self.capture.last_capture_milliseconds,
            detection_milliseconds,
        )
