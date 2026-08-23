"""Template-free detection of Stardew's post-catch treasure item menu."""

from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
from statistics import median

import cv2
import numpy as np

from ..domain import TreasureLootObservation


class TreasureLootDetector:
    """Locate source slots above the player's inventory in an item-grab menu.

    The detector intentionally knows only square-slot geometry and relative layout.
    It does not identify loot sprites or ship any game artwork. Calls are limited to
    encounters where the minigame already supplied strong treasure evidence.
    """

    def detect(self, frame: np.ndarray) -> TreasureLootObservation:
        """Return menu geometry and source slots which still appear occupied."""
        if frame.ndim != 3 or frame.shape[0] < 360 or frame.shape[1] < 640:
            return TreasureLootObservation(False)
        gray = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 45, 135)
        contours, _hierarchy = cv2.findContours(
            edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )
        boxes: list[tuple[int, int, int, int]] = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if not (
                40 <= width <= 78
                and 40 <= height <= 78
                and 0.82 <= width / max(1, height) <= 1.18
            ):
                continue
            rectangularity = cv2.contourArea(contour) / max(1.0, width * height)
            if rectangularity < 0.55:
                continue
            center = (x + width // 2, y + height // 2)
            if any(
                abs(center[0] - (bx + bw // 2)) <= 4
                and abs(center[1] - (by + bh // 2)) <= 4
                for bx, by, bw, bh in boxes
            ):
                continue
            boxes.append((x, y, width, height))

        rows: defaultdict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
        for box in sorted(boxes, key=lambda value: (value[1], value[0])):
            center_y = box[1] + box[3] // 2
            row_key = next((key for key in rows if abs(key - center_y) <= 6), center_y)
            rows[row_key].append(box)
        ordered_rows = [
            sorted(row, key=lambda box: box[0])
            for _key, row in sorted(rows.items())
            if len(row) >= 4
        ]
        if len(ordered_rows) < 4:
            return TreasureLootObservation(False)

        inventory_rows = ordered_rows[-3:]
        inventory_top = min(box[1] for row in inventory_rows for box in row)
        source_rows = [
            row
            for row in ordered_rows[:-3]
            if max(box[1] + box[3] for box in row) <= inventory_top - 18
        ]
        if not source_rows:
            return TreasureLootObservation(False)
        # Item sprites can cover an inner slot outline. Infer the complete
        # twelve-column lattice from a less-obscured row instead of requiring a
        # fixed number of visible rectangles in every row. This is particularly
        # important after a fifth hotbar item reduces the first backpack row to
        # only seven unobscured contours.
        source_boxes = self._infer_slot_boxes(source_rows)
        inventory_boxes = self._infer_slot_boxes(inventory_rows)
        if not source_boxes or not inventory_boxes:
            return TreasureLootObservation(False)
        source_centers = tuple(
            (x + width // 2, y + height // 2)
            for x, y, width, height in source_boxes
        )
        occupied = tuple(
            (x + width // 2, y + height // 2)
            for x, y, width, height in source_boxes
            if self._slot_is_occupied(frame, x, y, width, height)
        )
        occupied_inventory = sum(
            self._slot_is_occupied(frame, x, y, width, height)
            for x, y, width, height in inventory_boxes
        )
        last_inventory_x = max(x + width // 2 for x, _y, width, _height in inventory_boxes)
        bottom_inventory_y = max(y + height // 2 for _x, y, _width, height in inventory_boxes)
        close_candidates = [
            box
            for box in boxes
            if box[0] + box[2] // 2 >= last_inventory_x + 60
            and abs(box[1] + box[3] // 2 - bottom_inventory_y) <= 35
            and 55 <= box[2] <= 85
            and 55 <= box[3] <= 85
        ]
        close_button_center = None
        if close_candidates:
            close_box = max(close_candidates, key=lambda box: box[2] * box[3])
            close_button_center = (
                close_box[0] + close_box[2] // 2,
                close_box[1] + close_box[3] // 2,
            )
        alignment = min(1.0, min(len(row) for row in inventory_rows) / 12.0)
        confidence = min(1.0, 0.55 + alignment * 0.25 + len(source_boxes) / 60.0)
        return TreasureLootObservation(
            True,
            source_centers,
            occupied,
            confidence,
            len(inventory_boxes),
            occupied_inventory,
            close_button_center,
        )

    @staticmethod
    def _infer_slot_boxes(
        rows: list[list[tuple[int, int, int, int]]],
    ) -> list[tuple[int, int, int, int]]:
        """Reconstruct a 12-column slot grid from partially obscured contours."""
        best_run: list[tuple[int, int, int, int]] | None = None
        best_score = float("inf")
        for row in rows:
            ordered = sorted(row, key=lambda box: box[0] + box[2] / 2.0)
            for start in range(max(0, len(ordered) - 11)):
                run = ordered[start : start + 12]
                if len(run) != 12:
                    continue
                centers = [box[0] + box[2] / 2.0 for box in run]
                gaps = [right - left for left, right in pairwise(centers)]
                if not all(48.0 <= gap <= 72.0 for gap in gaps):
                    continue
                spacing = median(gaps)
                score = sum(abs(gap - spacing) for gap in gaps)
                if score < best_score:
                    best_score = score
                    best_run = run
        if best_run is None:
            return []

        centers_x = [box[0] + box[2] / 2.0 for box in best_run]
        width_pixels = max(1, round(median(box[2] for box in best_run)))
        height_pixels = max(1, round(median(box[3] for box in best_run)))
        inferred: list[tuple[int, int, int, int]] = []
        for row in rows:
            center_y = median(box[1] + box[3] / 2.0 for box in row)
            for center_x in centers_x:
                inferred.append(
                    (
                        round(center_x - width_pixels / 2.0),
                        round(center_y - height_pixels / 2.0),
                        width_pixels,
                        height_pixels,
                    )
                )
        return inferred

    @staticmethod
    def _slot_is_occupied(
        frame: np.ndarray,
        left_pixels: int,
        top_pixels: int,
        width_pixels: int,
        height_pixels: int,
    ) -> bool:
        """Distinguish a colorful/high-detail sprite from an empty slot interior."""
        inset_x = max(6, round(width_pixels * 0.20))
        inset_y = max(6, round(height_pixels * 0.20))
        interior = frame[
            top_pixels + inset_y : top_pixels + height_pixels - inset_y,
            left_pixels + inset_x : left_pixels + width_pixels - inset_x,
            :3,
        ]
        if interior.size == 0:
            return False
        gray = cv2.cvtColor(interior, cv2.COLOR_BGR2GRAY)
        detail_fraction = float(np.mean(cv2.Canny(gray, 45, 135) > 0))
        luminance_spread = float(np.std(gray))
        color_spread = float(np.max(np.std(interior, axis=(0, 1))))
        dark_fraction = float(np.mean(gray < 80))
        # The empty item-grab slots become strongly saturated orange at night,
        # so absolute saturation is not occupancy evidence. Item sprites retain
        # internal edges plus a dark outline while an empty slot is nearly
        # uniform. The Windows pointer's soft highlight has high luminance spread
        # but too little edge/dark coverage, so spread alone must not count it as
        # loot.
        return (
            detail_fraction >= 0.075
            or (
                dark_fraction >= 0.12
                and (luminance_spread >= 24.0 or color_spread >= 24.0)
            )
        )
