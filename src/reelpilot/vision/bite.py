"""HSV connected-component detection for Stardew's yellow bite icon."""

from __future__ import annotations

import cv2
import numpy as np

from ..domain import BiteObservation


class BiteDetector:
    """Detect the two bright connected components making up Stardew's bite icon."""

    def detect(self, frame: np.ndarray) -> BiteObservation:
        """Analyze a full game frame and return localized bite evidence."""
        height, width = frame.shape[:2]
        top = 32
        bottom = max(top, height - 120)
        left = 24
        right = max(left, width - 320)
        return self.detect_region(frame[top:bottom, left:right, :3], (left, top))

    def detect_region(
        self,
        region: np.ndarray,
        origin_pixels: tuple[int, int] = (0, 0),
    ) -> BiteObservation:
        """Analyze an already-cropped playable region without another image copy."""
        if region.size == 0:
            return BiteObservation(False)
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array((15, 110, 145), dtype=np.uint8),
            np.array((48, 255, 255), dtype=np.uint8),
        )
        count, _, stats, centroids = cv2.connectedComponentsWithStats(mask)
        strokes: list[tuple[int, int, int, int, int, float, float]] = []
        dots: list[tuple[int, int, int, int, int, float, float]] = []
        for label in range(1, count):
            x, y, component_width, component_height, area = (
                int(value) for value in stats[label]
            )
            center_x, center_y = (float(value) for value in centroids[label])
            component = (x, y, component_width, component_height, area, center_x, center_y)
            if 3 <= component_width <= 7 and 10 <= component_height <= 22 and 30 <= area <= 100:
                strokes.append(component)
            if 2 <= component_width <= 7 and 2 <= component_height <= 7 and 6 <= area <= 30:
                dots.append(component)
        for stroke in strokes:
            stroke_bottom = stroke[1] + stroke[3]
            for dot in dots:
                gap = dot[1] - stroke_bottom
                if abs(stroke[5] - dot[5]) <= 2.5 and 1 <= gap <= 6:
                    geometry = 1.0 - min(1.0, abs(stroke[5] - dot[5]) / 2.5) * 0.25
                    origin_x, origin_y = origin_pixels
                    left = origin_x + min(stroke[0], dot[0])
                    top = origin_y + stroke[1]
                    right = origin_x + max(
                        stroke[0] + stroke[2], dot[0] + dot[2]
                    )
                    bottom = origin_y + dot[1] + dot[3]
                    center = (
                        origin_x + (stroke[5] + dot[5]) / 2.0,
                        origin_y + (stroke[6] + dot[6]) / 2.0,
                    )
                    return BiteObservation(
                        True,
                        max(0.6, geometry),
                        (left, top, right, bottom),
                        center,
                    )
        return BiteObservation(False)
