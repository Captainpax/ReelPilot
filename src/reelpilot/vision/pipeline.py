"""Shared-capture vision pipeline routing frames to focused detectors."""

from __future__ import annotations

from time import perf_counter

import numpy as np

from ..domain import (
    BiteObservation,
    CastObservation,
    CatchObservation,
    EnergyObservation,
    FishingObservation,
    FoodPromptObservation,
    TreasureLootObservation,
    WindowBounds,
)
from ..platform.windows import find_stardew_window
from .bite import BiteDetector
from .capture import ScreenCapture
from .cast_meter import CastMeterDetector
from .catch_card import CatchCardDetector, CatchResultReader
from .energy import EnergyMeterDetector, FoodPromptDetector
from .fishing_ui import FishingUiDetector
from .treasure import TreasureLootDetector


class VisionPipeline:
    """Capture once and route frames through independently testable detectors."""

    def __init__(self, window_handle: int | None = None) -> None:
        """Bind to Stardew and create persistent capture and detector instances."""
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
        self.treasure_loot = TreasureLootDetector()
        self.energy = EnergyMeterDetector()
        self.food_prompt = FoodPromptDetector()
        self._last_full_frame: np.ndarray | None = None
        self._last_catch_card: np.ndarray | None = None
        self._closed = False

    @property
    def game_bounds(self) -> WindowBounds:
        """Return current game bounds from the capture owner."""
        return self.capture.bounds

    def observe_scene(
        self,
        expected_bar_length_pixels: int | None = None,
    ) -> FishingObservation:
        """Analyze a low-latency UI ROI, falling back to one full capture if stale."""
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
                return self._with_timings(result, started, used_roi_capture=True)
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
        """Capture the no-meter reference at the start of a charged cast."""
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        self.cast_meter.begin(frame)

    def observe_cast(self) -> CastObservation:
        """Capture and measure the active cast meter."""
        tracked = self.cast_meter.tracked_bounds
        if tracked is not None:
            game = self.game_bounds
            left, top, right, bottom = tracked
            absolute = WindowBounds(
                game.left_pixels + left,
                game.top_pixels + top,
                game.left_pixels + right,
                game.top_pixels + bottom,
            )
            crop = self.capture.capture_bounds(absolute)
            self._last_full_frame = crop
            measured = self.cast_meter.observe_tracked_crop(crop)
            if measured.meter_detected:
                return measured
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        return self.cast_meter.observe(frame)

    def observe_bite(self) -> BiteObservation:
        """Capture only the playable bite region and return its icon geometry."""
        game = self.game_bounds
        left_offset = 24
        top_offset = 32
        right_offset = max(left_offset, game.width_pixels - 320)
        bottom_offset = max(top_offset, game.height_pixels - 120)
        absolute = WindowBounds(
            game.left_pixels + left_offset,
            game.top_pixels + top_offset,
            game.left_pixels + right_offset,
            game.top_pixels + bottom_offset,
        )
        crop = self.capture.capture_bounds(absolute)
        self._last_full_frame = crop
        return self.bite.detect_region(crop, (left_offset, top_offset))

    def detect_text(self, expected_text: str) -> bool:
        """Use bounded English OCR for non-actionable MAX/Perfect verification."""
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        height, width = frame.shape[:2]
        region = frame[32 : max(33, height - 100), 24 : max(25, width - 250), :3]
        try:
            text = self.catch_reader._recognize_windows_ocr(region)
        except Exception:
            return False
        normalized = "".join(character for character in text.casefold() if character.isalpha())
        expected = "".join(
            character for character in expected_text.casefold() if character.isalpha()
        )
        return bool(expected and expected in normalized)

    def read_catch(self) -> CatchObservation:
        """Capture, localize, and conservatively OCR a result card."""
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        bounds = self.catch_card.locate(frame)
        if bounds is None:
            return CatchObservation(False)
        left, top, right, bottom = bounds
        card = frame[top:bottom, left:right, :3].copy()
        self._last_catch_card = card
        return self.catch_reader.read(card, bounds)

    def observe_treasure_loot(self) -> TreasureLootObservation:
        """Capture and inspect a possible fishing-treasure item menu."""
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        return self.treasure_loot.detect(frame)

    def observe_energy(self) -> EnergyObservation:
        """Capture and measure the right-side energy meter."""
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        return self.energy.detect(frame)

    def observe_food_prompt(self) -> FoodPromptObservation:
        """Capture and confirm an eating dialog before input is sent."""
        frame = self.capture.capture_frame()
        self._last_full_frame = frame
        return self.food_prompt.detect(frame)

    def latest_catch_card(self) -> np.ndarray | None:
        """Return the most recent lossless card crop without copying."""
        return self._last_catch_card

    def latest_frame(self) -> np.ndarray | None:
        """Return the most recent full frame or direct UI crop without copying."""
        return self._last_full_frame

    def close(self) -> None:
        """Close persistent capture resources idempotently."""
        if self._closed:
            return
        self._closed = True
        self.capture.close()

    def _with_timings(
        self,
        observation: FishingObservation,
        started_seconds: float,
        *,
        used_roi_capture: bool = False,
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
            observation.progress_confidence,
            self.capture.last_capture_milliseconds,
            detection_milliseconds,
            used_roi_capture,
            observation.fish_top_y_pixels,
            observation.fish_bottom_y_pixels,
            observation.bar_top_y_pixels,
            observation.bar_bottom_y_pixels,
            observation.containment_margin_pixels,
            observation.treasure_center_y_pixels,
            observation.treasure_top_y_pixels,
            observation.treasure_bottom_y_pixels,
            observation.treasure_confidence,
        )
