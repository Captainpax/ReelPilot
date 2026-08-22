from __future__ import annotations

import cv2
import numpy as np


class BiteDetector:
    """Detect the two bright connected components making up Stardew's bite icon."""

    def detect(self, frame: np.ndarray) -> tuple[bool, float]:
        height, width = frame.shape[:2]
        top = 32
        bottom = max(top, height - 120)
        left = 24
        right = max(left, width - 320)
        region = frame[top:bottom, left:right, :3]
        if region.size == 0:
            return False, 0.0
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
                    return True, max(0.6, geometry)
        return False, 0.0
