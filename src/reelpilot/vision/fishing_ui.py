from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import cv2
import numpy as np

from ..domain import FishingObservation


@dataclass(frozen=True, slots=True)
class UiBounds:
    left_pixels: int
    top_pixels: int
    right_pixels: int
    bottom_pixels: int


class FishingUiDetector:
    """Template-free fishing UI locator and motion-continuous track reader."""

    TARGET_WIDTH_PIXELS = 138
    TARGET_HEIGHT_PIXELS = 471
    TRACK_TOP_PIXELS = 22
    TRACK_BOTTOM_PIXELS = 445
    BAR_LEFT_PIXELS = 66
    BAR_RIGHT_PIXELS = 87

    def __init__(self) -> None:
        self.last_bounds: UiBounds | None = None
        self._last_fish_center_pixels: float | None = None
        self._last_bar_center_pixels: float | None = None

    def reset(self) -> None:
        self.last_bounds = None
        self._last_fish_center_pixels = None
        self._last_bar_center_pixels = None

    def locate(self, frame: np.ndarray) -> UiBounds | None:
        gray = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 45, 135)
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((13, 5), np.uint8))
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[tuple[float, UiBounds]] = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if not (75 <= width <= 175 and 390 <= height <= 525):
                continue
            if height / max(1, width) < 2.8:
                continue
            rectangularity = cv2.contourArea(contour) / max(1.0, width * height)
            if rectangularity < 0.25:
                continue
            center_x = x + width / 2
            center_y = y + height / 2
            left = round(center_x - self.TARGET_WIDTH_PIXELS / 2)
            top = round(center_y - self.TARGET_HEIGHT_PIXELS / 2)
            bounds = UiBounds(
                left, top, left + self.TARGET_WIDTH_PIXELS, top + self.TARGET_HEIGHT_PIXELS
            )
            if (
                left < 0
                or top < 0
                or bounds.right_pixels > frame.shape[1]
                or bounds.bottom_pixels > frame.shape[0]
            ):
                continue
            crop = self.crop(frame, bounds)
            structure = self._structure_confidence(crop)
            _, _, bar_confidence = self._detect_bar(crop, None)
            if bar_confidence < 0.45:
                continue
            score = structure + rectangularity * 0.2 + bar_confidence * 0.35
            candidates.append((score, bounds))
        if candidates:
            score, bounds = max(candidates, key=lambda item: item[0])
            if score >= 0.45:
                self.last_bounds = bounds
                return bounds
        fallback_bounds = self._locate_from_vertical_structure(frame, edges)
        if fallback_bounds is not None:
            self.last_bounds = fallback_bounds
        return fallback_bounds

    def _locate_from_vertical_structure(
        self, frame: np.ndarray, edges: np.ndarray
    ) -> UiBounds | None:
        # The panel can merge with the character, fishing line, or scenery in
        # a contour mask. Its tall rails remain independently visible. Map
        # those lines back to known structural offsets, then validate the
        # complete crop instead of depending on one outer contour.
        vertical = cv2.morphologyEx(edges, cv2.MORPH_OPEN, np.ones((120, 1), dtype=np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(vertical)
        structural_x_offsets = (1, 25, 61, 85, 106, 124, 133)
        candidates: list[tuple[float, UiBounds]] = []
        for label in range(1, count):
            x, y, _, height, _ = (int(value) for value in stats[label])
            if height < 160 or x < 40 or x > frame.shape[1] - 250:
                continue
            center_y = y + height / 2
            for x_offset in structural_x_offsets:
                left = x - x_offset
                for y_adjustment in (-8, -4, 0, 4, 8):
                    top = round(center_y - self.TARGET_HEIGHT_PIXELS / 2) + y_adjustment
                    bounds = UiBounds(
                        left,
                        top,
                        left + self.TARGET_WIDTH_PIXELS,
                        top + self.TARGET_HEIGHT_PIXELS,
                    )
                    if (
                        left < 0
                        or top < 0
                        or bounds.right_pixels > frame.shape[1]
                        or bounds.bottom_pixels > frame.shape[0]
                    ):
                        continue
                    crop = self.crop(frame, bounds)
                    structure = self._structure_confidence(crop)
                    if structure < 0.55:
                        continue
                    _, _, bar_confidence = self._detect_bar(crop, None)
                    if bar_confidence < 0.55:
                        continue
                    alignment = 1.0 - min(1.0, abs(y_adjustment) / 12.0)
                    score = (
                        structure
                        + bar_confidence * 0.45
                        + min(1.0, height / 420.0) * 0.20
                        + alignment * 0.03
                    )
                    candidates.append((score, bounds))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def analyze(
        self,
        crop: np.ndarray,
        expected_bar_length_pixels: int | None,
    ) -> FishingObservation:
        if crop.shape[:2] != (self.TARGET_HEIGHT_PIXELS, self.TARGET_WIDTH_PIXELS):
            return FishingObservation(False)
        bar_center, observed_length, bar_confidence = self._detect_bar(
            crop, expected_bar_length_pixels
        )
        fish_center, fish_confidence = self._detect_fish(crop, bar_center)
        progress = self._detect_progress(crop)
        if bar_confidence >= 0.55 and bar_center is not None:
            self._last_bar_center_pixels = bar_center
        if fish_confidence >= 0.55 and fish_center is not None:
            self._last_fish_center_pixels = fish_center
        return FishingObservation(
            True,
            fish_center,
            bar_center,
            observed_length,
            progress,
            fish_confidence,
            bar_confidence,
        )

    def is_valid_crop(self, crop: np.ndarray) -> bool:
        if crop.shape[:2] != (self.TARGET_HEIGHT_PIXELS, self.TARGET_WIDTH_PIXELS):
            return False
        if self._structure_confidence(crop) < 0.42:
            return False
        _, _, bar_confidence = self._detect_bar(crop, None)
        return bar_confidence >= 0.45

    @staticmethod
    def crop(frame: np.ndarray, bounds: UiBounds) -> np.ndarray:
        return frame[
            bounds.top_pixels : bounds.bottom_pixels,
            bounds.left_pixels : bounds.right_pixels,
        ]

    def _structure_confidence(self, crop: np.ndarray) -> float:
        lane = crop[self.TRACK_TOP_PIXELS : self.TRACK_BOTTOM_PIXELS, 55:98, :3]
        if lane.size == 0:
            return 0.0
        gray = cv2.cvtColor(lane, cv2.COLOR_BGR2GRAY)
        vertical_edges = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
        edge_strength = float(np.mean(np.abs(vertical_edges) > 45))
        hsv = cv2.cvtColor(lane, cv2.COLOR_BGR2HSV)
        saturated = float(np.mean(hsv[:, :, 1] > 45))
        return min(1.0, edge_strength * 3.5 + saturated * 0.45)

    def _detect_bar(
        self,
        crop: np.ndarray,
        expected_length_pixels: int | None,
    ) -> tuple[float | None, int | None, float]:
        lane = crop[
            self.TRACK_TOP_PIXELS : self.TRACK_BOTTOM_PIXELS,
            self.BAR_LEFT_PIXELS : self.BAR_RIGHT_PIXELS,
            :3,
        ]
        hsv = cv2.cvtColor(lane, cv2.COLOR_BGR2HSV)
        green = (
            cv2.inRange(
                hsv, np.array((35, 45, 35), np.uint8), np.array((100, 255, 255), np.uint8)
            )
            > 0
        )
        row_evidence = np.mean(green, axis=1)
        if expected_length_pixels is None:
            # The bar covers most of the lane width. The fish sprite can be
            # green too, but normally contributes a shorter adjacent run;
            # broad morphological closing incorrectly welds that sprite onto
            # the bar and makes its measured length change with fish motion.
            active = row_evidence >= 0.65
            starts = np.flatnonzero(np.diff(np.pad(active.astype(np.int8), (1, 1))) == 1)
            ends = np.flatnonzero(np.diff(np.pad(active.astype(np.int8), (1, 1))) == -1)
            raw_runs = [(int(a), int(b)) for a, b in zip(starts, ends, strict=True)]
            runs = [(start, end) for start, end in raw_runs if end - start >= 45]
            for first, second in pairwise(raw_runs):
                first_length = first[1] - first[0]
                second_length = second[1] - second[0]
                gap = second[0] - first[1]
                span = second[1] - first[0]
                if (
                    first_length >= 15
                    and second_length >= 15
                    and gap <= 28
                    and 45 <= span <= 190
                ):
                    runs.append((first[0], second[1]))
            if not runs:
                return None, None, 0.0
            top, bottom = max(
                runs,
                key=lambda run: (
                    float(np.mean(row_evidence[run[0] : run[1]]))
                    + min(1.0, (run[1] - run[0]) / 90.0) * 0.25
                ),
            )
            observed_length = bottom - top
        else:
            observed_length = expected_length_pixels
            candidates: list[tuple[float, int]] = []
            for top in range(0, len(row_evidence) - expected_length_pixels + 1):
                center = top + expected_length_pixels / 2
                score = float(np.mean(row_evidence[top : top + expected_length_pixels]))
                if self._last_bar_center_pixels is not None:
                    score -= (
                        abs(center + self.TRACK_TOP_PIXELS - self._last_bar_center_pixels)
                        / expected_length_pixels
                        * 0.18
                    )
                candidates.append((score, top))
            if not candidates:
                return None, None, 0.0
            score, top = max(candidates)
            bottom = top + expected_length_pixels
            if score < 0.04:
                return None, observed_length, 0.0
        confidence = min(
            1.0,
            float(np.mean(row_evidence[top:bottom])) * 1.7
            + float(np.mean(row_evidence[top:bottom] >= 0.15)) * 0.55,
        )
        center_pixels = self.TRACK_TOP_PIXELS + top + (bottom - top) / 2
        if (
            self._last_bar_center_pixels is not None
            and abs(center_pixels - self._last_bar_center_pixels) > 64
        ):
            confidence *= 0.2
        return center_pixels, observed_length, confidence

    def _detect_fish(
        self,
        crop: np.ndarray,
        bar_center_pixels: float | None,
    ) -> tuple[float | None, float]:
        del bar_center_pixels
        # The fish sprite adds more horizontal edge energy than the repeating
        # track rails. This weighted row scan finds that energy without
        # embedding or matching any game artwork.
        region = crop[self.TRACK_TOP_PIXELS : self.TRACK_BOTTOM_PIXELS, 54:101, :3]
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 55, 150)
        row_energy = np.count_nonzero(edges, axis=1).astype(np.float32)
        weighted_energy = np.convolve(row_energy, np.hanning(33), mode="same")
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        fish_colors = cv2.inRange(
            hsv,
            np.array((35, 120, 100), dtype=np.uint8),
            np.array((115, 255, 255), dtype=np.uint8),
        )
        color_energy = np.count_nonzero(fish_colors, axis=1).astype(np.float32)
        weighted_color = np.convolve(color_energy, np.hanning(25), mode="same")
        if weighted_energy.size == 0 or float(np.max(weighted_color)) <= 0.0:
            return None, 0.0
        normalized_color = weighted_color / float(np.max(weighted_color))
        combined_energy = weighted_energy * (0.15 + normalized_color)
        peak_index = int(np.argmax(combined_energy))
        peak = float(combined_energy[peak_index])
        background = float(np.percentile(combined_energy, 80))
        contrast = max(0.0, (peak - background) / max(1.0, peak))
        if contrast < 0.12:
            return None, min(0.5, contrast * 2.5)
        center = float(self.TRACK_TOP_PIXELS + peak_index + 9)
        confidence = min(1.0, 0.55 + contrast * 0.90)
        if (
            self._last_fish_center_pixels is not None
            and abs(center - self._last_fish_center_pixels) > 64
        ):
            confidence *= 0.25
        return center, confidence

    @staticmethod
    def _detect_progress(crop: np.ndarray) -> float:
        channel = crop[18:451, 108:119, :3]
        if channel.size == 0:
            return 0.0
        hsv = cv2.cvtColor(channel, cv2.COLOR_BGR2HSV)
        # The progress fill begins gold and turns green as the catch nears
        # completion. Brown rails remain saturated too, so brightness alone
        # makes an empty meter look full. Hue-separated gold/green evidence
        # preserves the early reading and, critically, recognizes a full
        # green channel instead of reporting zero at the moment of a catch.
        gold = (
            (hsv[:, :, 0] >= 5)
            & (hsv[:, :, 0] <= 35)
            & (hsv[:, :, 1] >= 180)
            & (hsv[:, :, 2] >= 140)
        )
        green = (
            (hsv[:, :, 0] >= 35)
            & (hsv[:, :, 0] <= 95)
            & (hsv[:, :, 1] >= 180)
            & (hsv[:, :, 2] >= 140)
        )
        active_rows = np.mean(gold | green, axis=1) >= 0.60
        return max(0.0, min(1.0, float(np.mean(active_rows))))
