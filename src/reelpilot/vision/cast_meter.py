from __future__ import annotations

import cv2
import numpy as np

from ..domain import CastObservation


class CastMeterDetector:
    MIN_WIDTH_PIXELS = 130
    MAX_WIDTH_PIXELS = 155
    MIN_HEIGHT_PIXELS = 30
    MAX_HEIGHT_PIXELS = 42

    def __init__(self) -> None:
        self._reference_gray: np.ndarray | None = None
        self._pending_bounds: tuple[int, int, int, int] | None = None
        self._tracked_bounds: tuple[int, int, int, int] | None = None

    def begin(self, frame: np.ndarray) -> None:
        self._reference_gray = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY)
        self._pending_bounds = None
        self._tracked_bounds = None

    def reset(self) -> None:
        self._reference_gray = None
        self._pending_bounds = None
        self._tracked_bounds = None

    def observe(self, frame: np.ndarray) -> CastObservation:
        if self._tracked_bounds is not None:
            measured = self._measure(frame, self._tracked_bounds)
            if measured.confidence >= 0.50:
                return measured
            self._tracked_bounds = None

        candidate = self._acquire(frame)
        if candidate is None:
            return CastObservation(False)
        if (
            self._pending_bounds is not None
            and self._near(candidate, self._pending_bounds)
            and self._edge_confidence(frame, candidate) >= 0.55
        ):
            self._tracked_bounds = candidate
            return self._measure(frame, candidate)
        self._pending_bounds = candidate
        return CastObservation(False, bounds=candidate, confidence=0.35)

    def _acquire(self, frame: np.ndarray) -> tuple[int, int, int, int] | None:
        if self._reference_gray is None or self._reference_gray.shape != frame.shape[:2]:
            return None
        gray = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY)
        difference = cv2.absdiff(gray, self._reference_gray)
        _, mask = cv2.threshold(difference, 22, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 5), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        best: tuple[float, tuple[int, int, int, int]] | None = None
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if not (
                self.MIN_WIDTH_PIXELS <= width <= self.MAX_WIDTH_PIXELS
                and self.MIN_HEIGHT_PIXELS <= height <= self.MAX_HEIGHT_PIXELS
            ):
                continue
            rectangularity = cv2.contourArea(contour) / max(1.0, width * height)
            if rectangularity < 0.72:
                continue
            bounds = (x, y, x + width, y + height)
            edge_confidence = self._edge_confidence(frame, bounds)
            if edge_confidence < 0.45:
                continue
            score = rectangularity * 0.55 + edge_confidence * 0.45
            if best is None or score > best[0]:
                best = (score, bounds)
        fill_candidate = self._acquire_from_fill(frame)
        if fill_candidate is not None:
            fill_score = self._edge_confidence(frame, fill_candidate)
            if best is None or fill_score > best[0]:
                best = (fill_score, fill_candidate)
        return best[1] if best else None

    def _acquire_from_fill(self, frame: np.ndarray) -> tuple[int, int, int, int] | None:
        hsv = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            # Stardew applies a strong blue nighttime tint to the complete
            # scene. The cast fill keeps its saturation and hue, but its
            # value can fall into the 40s. Geometry and the surrounding
            # meter-edge validation provide the false-positive protection.
            np.array((20, 100, 35), dtype=np.uint8),
            np.array((95, 255, 255), dtype=np.uint8),
        )
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        frame_height, frame_width = frame.shape[:2]
        best: tuple[float, tuple[int, int, int, int]] | None = None
        for label in range(1, count):
            x, y, width, height, area = (int(value) for value in stats[label])
            if not (4 <= width <= 142 and 10 <= height <= 26 and area >= 55):
                continue
            if x >= frame_width - 250 or y >= frame_height - 100:
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
            (hsv[:, :, 0] >= 20)
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
        charge = fill_width / max(1, len(bridged))
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
            len(bridged),
            edge_confidence,
        )

    @staticmethod
    def _near(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
        return all(abs(a - b) <= 4 for a, b in zip(first, second, strict=True))

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
