"""Template-free energy-meter and eating-prompt detection."""

from __future__ import annotations

import asyncio
import re

import cv2
import numpy as np

from ..domain import EnergyObservation, FoodPromptObservation


class EnergyMeterDetector:
    """Measure Stardew's bottom-anchored right-HUD energy fill."""

    def detect(self, frame: np.ndarray) -> EnergyObservation:
        """Return the best vertical meter geometry and normalized fill."""
        height, width = frame.shape[:2]
        if height < 300 or width < 500:
            return EnergyObservation(False)
        region_left = max(0, width - 115)
        region_top = max(30, int(height * 0.12))
        region = frame[region_top : height - 8, region_left:width, :3]
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        brown = cv2.inRange(
            hsv,
            np.array((3, 75, 45), np.uint8),
            np.array((32, 255, 245), np.uint8),
        )
        brown = cv2.morphologyEx(
            brown, cv2.MORPH_CLOSE, np.ones((13, 7), np.uint8)
        )
        contours, _ = cv2.findContours(
            brown, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        best: tuple[float, tuple[int, int, int, int]] | None = None
        for contour in contours:
            x, y, meter_width, meter_height = cv2.boundingRect(contour)
            if not (18 <= meter_width <= 62 and 135 <= meter_height <= 310):
                continue
            if meter_height < meter_width * 3.5 or x < region.shape[1] - 82:
                continue
            rectangularity = cv2.contourArea(contour) / max(
                1.0, meter_width * meter_height
            )
            bottom_alignment = 1.0 - abs(
                (y + meter_height) - region.shape[0]
            ) / max(1.0, region.shape[0])
            right_alignment = (x + meter_width) / max(1.0, region.shape[1])
            score = (
                rectangularity * 0.45
                + bottom_alignment * 0.35
                + right_alignment * 0.20
            )
            if best is None or score > best[0]:
                best = (score, (x, y, meter_width, meter_height))
        if best is None:
            return EnergyObservation(False)

        geometry_score, (x, y, meter_width, meter_height) = best
        meter_hsv = hsv[y : y + meter_height, x : x + meter_width]
        meter_brown = cv2.inRange(
            meter_hsv,
            np.array((3, 75, 45), np.uint8),
            np.array((32, 255, 245), np.uint8),
        )
        horizontal_rails = np.mean(meter_brown > 0, axis=1) >= 0.75
        rail_changes = np.diff(
            np.pad(horizontal_rails.astype(np.int8), (1, 1))
        )
        rail_starts = np.flatnonzero(rail_changes == 1)
        rail_ends = np.flatnonzero(rail_changes == -1)
        rail_runs = list(zip(rail_starts.tolist(), rail_ends.tolist(), strict=True))
        upper_rails = [run for run in rail_runs if run[1] <= meter_height * 0.50]
        lower_rails = [run for run in rail_runs if run[0] >= meter_height * 0.60]
        inset_x = max(4, round(meter_width * 0.24))
        track_top = upper_rails[-1][1] if upper_rails else round(meter_height * 0.10)
        track_bottom = lower_rails[0][0] if lower_rails else round(meter_height * 0.94)
        inner = hsv[
            y + track_top : y + track_bottom,
            x + inset_x : x + meter_width - inset_x,
        ]
        if inner.size == 0 or inner.shape[0] < 50 or inner.shape[1] < 4:
            return EnergyObservation(False)

        green = cv2.inRange(
            inner,
            # Stardew's nighttime tint can reduce the visible green fill to V=64
            # at 75% scale while hue/saturation remain distinctive.
            np.array((35, 95, 42), np.uint8),
            np.array((100, 255, 255), np.uint8),
        )
        yellow = cv2.inRange(
            inner,
            # The empty rail is orange at S=118-132 in the live nighttime
            # captures. Real yellow energy remains strongly saturated.
            np.array((15, 180, 100), np.uint8),
            np.array((34, 255, 255), np.uint8),
        )
        red = cv2.inRange(
            inner,
            np.array((0, 180, 100), np.uint8),
            np.array((14, 255, 255), np.uint8),
        )
        fill = cv2.bitwise_or(green, cv2.bitwise_or(yellow, red))
        row_evidence = np.count_nonzero(fill, axis=1) >= max(
            2, round(inner.shape[1] * 0.40)
        )
        # Fill is anchored to the bottom. Bridge up to two lighting gaps without
        # accepting a detached HUD decoration above the empty meter.
        filled_rows = 0
        gaps = 0
        started = False
        for present in reversed(row_evidence.tolist()):
            if present:
                started = True
                filled_rows += 1 + gaps
                gaps = 0
            elif not started:
                gaps += 1
                if gaps > 5:
                    gaps = 0
                    break
            elif gaps < 2:
                gaps += 1
            else:
                break
        fill_ratio = min(1.0, max(0.0, filled_rows / inner.shape[0]))
        confidence = min(
            1.0,
            0.50
            + max(0.0, geometry_score) * 0.30
            + (0.20 if filled_rows else 0.10),
        )
        bounds = (
            region_left + x,
            region_top + y,
            region_left + x + meter_width,
            region_top + y + meter_height,
        )
        return EnergyObservation(True, fill_ratio, confidence, bounds)


class FoodPromptDetector:
    """Confirm an English eating dialog and locate its visible Yes word."""

    def detect(self, frame: np.ndarray) -> FoodPromptObservation:
        """Return a prompt only when both eating text and Yes geometry agree."""
        bounds = self._locate_dialog(frame)
        if bounds is None:
            return FoodPromptObservation(False)
        left, top, right, bottom = bounds
        crop = frame[top:bottom, left:right, :3]
        try:
            words = self._recognize_words(crop)
        except Exception:
            return FoodPromptObservation(False)
        normalized = [re.sub(r"[^a-z]", "", text.casefold()) for text, _ in words]
        if not any(word == "eat" or word.startswith("eat") for word in normalized):
            return FoodPromptObservation(False)
        yes_candidates = [
            box
            for word, (_text, box) in zip(normalized, words, strict=True)
            if word == "yes"
        ]
        if not yes_candidates:
            return FoodPromptObservation(False)
        x, y, word_width, word_height = yes_candidates[-1]
        yes_center = (left + x + word_width // 2, top + y + word_height // 2)
        return FoodPromptObservation(True, 0.95, bounds, yes_center)

    @staticmethod
    def _locate_dialog(frame: np.ndarray) -> tuple[int, int, int, int] | None:
        hsv = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2HSV)
        beige = cv2.inRange(
            hsv,
            np.array((5, 25, 130), np.uint8),
            np.array((38, 210, 255), np.uint8),
        )
        beige = cv2.morphologyEx(
            beige, cv2.MORPH_CLOSE, np.ones((15, 21), np.uint8)
        )
        contours, _ = cv2.findContours(
            beige, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        height, width = frame.shape[:2]
        best: tuple[float, tuple[int, int, int, int]] | None = None
        for contour in contours:
            x, y, panel_width, panel_height = cv2.boundingRect(contour)
            if not (
                width * 0.28 <= panel_width <= width * 0.92
                and 80 <= panel_height <= height * 0.55
            ):
                continue
            center_offset = abs((x + panel_width / 2) - width / 2) / width
            rectangularity = cv2.contourArea(contour) / max(
                1.0, panel_width * panel_height
            )
            if center_offset > 0.20 or rectangularity < 0.45:
                continue
            score = rectangularity - center_offset
            if best is None or score > best[0]:
                best = (score, (x, y, x + panel_width, y + panel_height))
        return best[1] if best is not None else None

    @staticmethod
    def _recognize_words(
        crop: np.ndarray,
    ) -> tuple[tuple[str, tuple[int, int, int, int]], ...]:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter

        rgba = cv2.cvtColor(crop, cv2.COLOR_BGR2RGBA)
        writer = DataWriter()
        writer.write_bytes(rgba.tobytes())
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.RGBA8, rgba.shape[1], rgba.shape[0]
        )
        engine = OcrEngine.try_create_from_language(Language("en-US"))
        if engine is None:
            raise RuntimeError("Windows English OCR is unavailable")

        async def recognize() -> tuple[tuple[str, tuple[int, int, int, int]], ...]:
            result = await engine.recognize_async(bitmap)
            recognized: list[tuple[str, tuple[int, int, int, int]]] = []
            for line in result.lines:
                for word in line.words:
                    box = word.bounding_rect
                    recognized.append(
                        (
                            word.text,
                            (
                                round(box.x),
                                round(box.y),
                                round(box.width),
                                round(box.height),
                            ),
                        )
                    )
            return tuple(recognized)

        return asyncio.run(recognize())
