"""Background-independent acquisition and fixed-bound tracking of the cast meter."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import pairwise

import cv2
import numpy as np

from ..domain import CastObservation


@dataclass(slots=True)
class _CandidateTrack:
    """Hold short-lived temporal evidence for one possible cast meter."""

    bounds: tuple[int, int, int, int]
    charges: deque[float] = field(default_factory=lambda: deque(maxlen=4))
    observations: int = 0
    last_seen_sequence: int = 0


class CastMeterDetector:
    """Acquire a new rectangular meter, then measure its left-anchored fill."""

    MIN_WIDTH_PIXELS = 130
    MAX_WIDTH_PIXELS = 155
    MIN_HEIGHT_PIXELS = 30
    MAX_HEIGHT_PIXELS = 42

    def __init__(self) -> None:
        """Create a detector with no reference frame or tracked bounds."""
        self._reference_gray: np.ndarray | None = None
        self._candidates: list[_CandidateTrack] = []
        self._sequence = 0
        self._tracked_bounds: tuple[int, int, int, int] | None = None

    @property
    def tracked_bounds(self) -> tuple[int, int, int, int] | None:
        """Return fixed game-relative bounds after temporal meter lock."""
        return self._tracked_bounds

    def begin(self, frame: np.ndarray) -> None:
        """Use ``frame`` as the no-meter reference for a new cast."""
        self._reference_gray = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY)
        self._candidates.clear()
        self._sequence = 0
        self._tracked_bounds = None

    def reset(self) -> None:
        """Forget reference, pending, and fixed tracked bounds."""
        self._reference_gray = None
        self._candidates.clear()
        self._sequence = 0
        self._tracked_bounds = None

    def observe(self, frame: np.ndarray) -> CastObservation:
        """Acquire or track the meter and return normalized charge evidence."""
        self._sequence += 1
        if self._tracked_bounds is not None:
            measured = self._measure(frame, self._tracked_bounds)
            if measured.confidence >= 0.50:
                return measured
            self._tracked_bounds = None

        candidate_bounds = self._acquire_candidates(frame)
        if not candidate_bounds:
            self._candidates = [
                item
                for item in self._candidates
                if self._sequence - item.last_seen_sequence <= 2
            ]
            return CastObservation(False)
        best_pending: CastObservation | None = None
        for bounds in candidate_bounds[:4]:
            measurement = self._measure(frame, bounds)
            track = next(
                (item for item in self._candidates if self._near(item.bounds, bounds)),
                None,
            )
            if track is None:
                track = _CandidateTrack(bounds)
                self._candidates.append(track)
            else:
                track.bounds = bounds
            track.observations += 1
            track.last_seen_sequence = self._sequence
            track.charges.append(measurement.charge_ratio)
            values = tuple(track.charges)
            increases = sum(
                current - previous >= 0.02
                for previous, current in pairwise(values)
            )
            if (
                track.observations >= 3
                and measurement.tracking_confidence >= 0.55
                and increases >= 2
            ):
                self._tracked_bounds = bounds
                self._candidates.clear()
                return measurement
            if best_pending is None or measurement.tracking_confidence > (
                best_pending.tracking_confidence
            ):
                best_pending = CastObservation(
                    False,
                    bounds=bounds,
                    confidence=0.35,
                    tracking_confidence=measurement.tracking_confidence,
                )
        self._candidates = sorted(
            (
                item
                for item in self._candidates
                if self._sequence - item.last_seen_sequence <= 2
            ),
            key=lambda item: (item.last_seen_sequence, item.observations),
            reverse=True,
        )[:4]
        return best_pending or CastObservation(False)

    def observe_tracked_crop(self, crop: np.ndarray) -> CastObservation:
        """Measure a direct fixed-bounds crop after acquisition has locked."""
        bounds = self._tracked_bounds
        if bounds is None:
            return CastObservation(False)
        local_bounds = (0, 0, crop.shape[1], crop.shape[0])
        measured = self._measure(crop, local_bounds)
        if measured.confidence < 0.50:
            return CastObservation(False, bounds=bounds)
        return CastObservation(
            measured.meter_detected,
            measured.charge_ratio,
            measured.confidence,
            bounds,
            measured.fill_width_pixels,
            measured.track_width_pixels,
            measured.tracking_confidence,
        )

    def _acquire_candidates(
        self, frame: np.ndarray
    ) -> list[tuple[int, int, int, int]]:
        if self._reference_gray is None or self._reference_gray.shape != frame.shape[:2]:
            return []
        gray = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY)
        difference = cv2.absdiff(gray, self._reference_gray)
        _, mask = cv2.threshold(difference, 22, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 5), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[tuple[float, tuple[int, int, int, int]]] = []
        frame_height, frame_width = frame.shape[:2]
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if not (
                self.MIN_WIDTH_PIXELS <= width <= self.MAX_WIDTH_PIXELS
                and self.MIN_HEIGHT_PIXELS <= height <= self.MAX_HEIGHT_PIXELS
            ):
                continue
            rectangularity = cv2.contourArea(contour) / max(1.0, width * height)
            if rectangularity < 0.80:
                continue
            bounds = (x, y, x + width, y + height)
            if self._outside_playable_region(bounds, frame_width, frame_height):
                continue
            edge_confidence = self._edge_confidence(frame, bounds)
            if edge_confidence < 0.45:
                continue
            score = rectangularity * 0.55 + edge_confidence * 0.45
            candidates.append((score, bounds))
        fill_candidate = self._acquire_from_fill(frame)
        if fill_candidate is not None:
            fill_score = self._edge_confidence(frame, fill_candidate)
            candidates.append((fill_score, fill_candidate))
        candidates.sort(key=lambda item: item[0], reverse=True)
        unique: list[tuple[int, int, int, int]] = []
        for _score, bounds in candidates:
            if not any(self._near(bounds, present, tolerance_pixels=8) for present in unique):
                unique.append(bounds)
        return unique[:4]

    def _acquire_from_fill(self, frame: np.ndarray) -> tuple[int, int, int, int] | None:
        hsv = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            # Stardew applies a strong blue nighttime tint to the complete
            # scene. The cast fill keeps its saturation and hue, but its
            # value can fall into the 40s. Geometry and the surrounding
            # meter-edge validation provide the false-positive protection.
            np.array((25, 100, 35), dtype=np.uint8),
            np.array((95, 255, 255), dtype=np.uint8),
        )
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        frame_height, frame_width = frame.shape[:2]
        best: tuple[float, tuple[int, int, int, int]] | None = None
        for label in range(1, count):
            x, y, width, height, area = (int(value) for value in stats[label])
            if not (4 <= width <= 142 and 10 <= height <= 26 and area >= 55):
                continue
            proposed = (x - 8, y - 7, x - 8 + 138, y - 7 + 35)
            if self._outside_playable_region(proposed, frame_width, frame_height):
                continue
            if area / max(1, width * height) < 0.60:
                continue
            outer_height = min(self.MAX_HEIGHT_PIXELS, max(self.MIN_HEIGHT_PIXELS, height + 14))
            for left_inset in range(5, 10):
                left = x - left_inset
                for top_inset in range(5, 9):
                    top = y - top_inset
                    for outer_width in (134, 136, 138, 140, 142):
                        bounds = (left, top, left + outer_width, top + outer_height)
                        if (
                            left < 0
                            or top < 0
                            or bounds[2] > frame_width
                            or bounds[3] > frame_height
                        ):
                            continue
                        confidence = self._edge_confidence(frame, bounds)
                        if best is None or confidence > best[0]:
                            best = (confidence, bounds)
        return best[1] if best is not None and best[0] >= 0.48 else None

    @staticmethod
    def _outside_playable_region(
        bounds: tuple[int, int, int, int], frame_width: int, frame_height: int
    ) -> bool:
        """Reject title-bar, toolbar, and right-HUD rectangles before ranking."""
        left, top, right, bottom = bounds
        right_margin = 320 if frame_width >= 900 else 40
        bottom_margin = 120 if frame_height >= 600 else 40
        return (
            left < 24
            or top < 32
            or right > frame_width - right_margin
            or bottom > frame_height - bottom_margin
        )

    def _measure(
        self,
        frame: np.ndarray,
        bounds: tuple[int, int, int, int],
    ) -> CastObservation:
        left, top, right, bottom = bounds
        outer = frame[top:bottom, left:right, :3]
        if outer.size == 0:
            return CastObservation(False, bounds=bounds)
        horizontal_inset = max(3, round((right - left) * 0.06))
        vertical_inset = max(3, round((bottom - top) * 0.25))
        inner = outer[vertical_inset:-vertical_inset, horizontal_inset:-horizontal_inset]
        if inner.size == 0:
            return CastObservation(False, bounds=bounds)
        hsv = cv2.cvtColor(inner, cv2.COLOR_BGR2HSV)
        bright_fill = (hsv[:, :, 1] >= 120) & (hsv[:, :, 2] >= 120)
        nighttime_fill = (
            (hsv[:, :, 0] >= 25)
            & (hsv[:, :, 0] <= 95)
            & (hsv[:, :, 1] >= 100)
            & (hsv[:, :, 2] >= 35)
        )
        evidence = np.mean(bright_fill | nighttime_fill, axis=0) >= 0.60
        bridged = evidence.copy()
        for index in range(1, len(bridged) - 1):
            if not bridged[index] and bridged[index - 1] and bridged[index + 1]:
                bridged[index] = True
        run_start = next(
            (index for index in range(min(3, len(bridged))) if bridged[index]), None
        )
        fill_end = run_start
        if run_start is not None:
            while fill_end is not None and fill_end < len(bridged) and bridged[fill_end]:
                fill_end += 1
        fill_width = 0 if fill_end is None else fill_end
        # The normalized inner ROI still contains Stardew's three-pixel right
        # endpoint/divider. A genuinely full fill ends immediately before that
        # fixed cap; counting it as charge space capped live readings at 97.46%
        # and made the meter cycle past MAX. Keep the raw fill width for telemetry,
        # but normalize against the reachable slider endpoint.
        usable_track_width = max(1, len(bridged) - 3)
        # Night tint and the moving divider can hide the final one or two fill
        # columns. Private MAX frames repeatedly measured 113/115 even though the
        # visible slider had reached its endpoint. Apply tolerance only at that
        # endpoint; lower charges retain their raw spatial measurement.
        endpoint_tolerance_pixels = (
            2 if fill_width >= usable_track_width - 2 else 0
        )
        charge = min(
            1.0,
            (fill_width + endpoint_tolerance_pixels) / usable_track_width,
        )
        edge_confidence = self._edge_confidence(frame, bounds)
        confidence = (
            min(1.0, 0.55 + edge_confidence * 0.35) if fill_width else edge_confidence * 0.5
        )
        return CastObservation(
            True,
            charge,
            confidence,
            bounds,
            fill_width,
            usable_track_width,
            edge_confidence,
        )

    @staticmethod
    def _near(
        first: tuple[int, int, int, int],
        second: tuple[int, int, int, int],
        *,
        tolerance_pixels: int = 4,
    ) -> bool:
        return all(
            abs(a - b) <= tolerance_pixels for a, b in zip(first, second, strict=True)
        )

    @staticmethod
    def _edge_confidence(frame: np.ndarray, bounds: tuple[int, int, int, int]) -> float:
        left, top, right, bottom = bounds
        crop = frame[top:bottom, left:right, :3]
        if crop.size == 0:
            return 0.0
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        # Keep normal-scene edge thresholds, but adapt to the uniformly dark
        # nighttime overlay where the same meter border tops out below gray
        # value 60. This is local to an already geometry-sized candidate.
        if float(np.percentile(gray, 95)) < 100.0:
            edges = cv2.Canny(gray, 15, 45) > 0
        else:
            edges = cv2.Canny(gray, 50, 150) > 0
        band_height = min(6, edges.shape[0] // 2)
        band_width = min(6, edges.shape[1] // 2)
        if band_height == 0 or band_width == 0:
            return 0.0
        top_coverage = float(np.mean(np.any(edges[:band_height], axis=0)))
        bottom_coverage = float(np.mean(np.any(edges[-band_height:], axis=0)))
        left_coverage = float(np.mean(np.any(edges[:, :band_width], axis=1)))
        right_coverage = float(np.mean(np.any(edges[:, -band_width:], axis=1)))
        horizontal = min(top_coverage, bottom_coverage) / 0.65
        vertical = min(left_coverage, right_coverage) / 0.35
        return min(1.0, max(0.0, horizontal) * 0.65 + max(0.0, vertical) * 0.35)
