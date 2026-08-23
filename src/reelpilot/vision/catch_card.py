"""Locate catch-result cards and read conservative English text with Windows OCR."""

from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher

import cv2
import numpy as np

from ..domain import CatchObservation, RecognitionStatus, ResultType
from ..stats.catalog import FISH_NAMES, ITEM_NAMES

LENGTH_PATTERN = re.compile(
    r"(?:Length\s*[.:;\]]?\s*)?(\d{1,3})\s*in\.?",
    re.IGNORECASE,
)
QUANTITY_PATTERN = re.compile(r"(?:x|×)\s*(\d+)$", re.IGNORECASE)


class CatchCardDetector:
    """Locate purple fish cards or white instant-item bubbles by geometry."""

    def locate(self, frame: np.ndarray) -> tuple[int, int, int, int] | None:
        """Return the best result-card bounds or ``None`` when no card is present."""
        hsv = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array((112, 30, 90), np.uint8),
            np.array((160, 210, 255), np.uint8),
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best: tuple[float, tuple[int, int, int, int]] | None = None
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if not (150 <= width <= 700 and 65 <= height <= 430):
                continue
            area_ratio = cv2.contourArea(contour) / max(1.0, width * height)
            center_distance = abs((x + width / 2) - frame.shape[1] / 2) / frame.shape[1]
            score = area_ratio - center_distance * 0.25
            if area_ratio >= 0.50 and (best is None or score > best[0]):
                best = (score, (x, y, x + width, y + height))
        if best is not None:
            return best[1]
        return self._locate_item_bubble(hsv)

    @staticmethod
    def _locate_item_bubble(hsv: np.ndarray) -> tuple[int, int, int, int] | None:
        # The live 1.6.15 result is a near-white speech bubble. A permissive beige
        # mask connects it to the fish-shop facade behind the player, producing a
        # single oversized contour. The bright/low-saturation paper color remains
        # stable through nighttime lighting because UI pixels are not world-tinted.
        mask = cv2.inRange(
            hsv,
            np.array((0, 0, 245), dtype=np.uint8),
            np.array((179, 55, 255), dtype=np.uint8),
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 21), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        frame_height, frame_width = hsv.shape[:2]
        best: tuple[float, tuple[int, int, int, int]] | None = None
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if not (130 <= width <= 420 and 60 <= height <= 260):
                continue
            if y < 25 or y + height > frame_height - 50:
                continue
            rectangularity = cv2.contourArea(contour) / max(1.0, width * height)
            center_distance = abs((x + width / 2) - frame_width / 2) / frame_width
            if rectangularity < 0.52 or center_distance > 0.32:
                continue
            score = rectangularity - center_distance * 0.30
            if best is None or score > best[0]:
                best = (score, (x, y, x + width, y + height))
        return best[1] if best else None


class CatchResultReader:
    """Read English result text conservatively and reject ambiguous names."""

    def read(
        self,
        card: np.ndarray,
        bounds: tuple[int, int, int, int],
    ) -> CatchObservation:
        """Classify OCR text, length, and quantity from a localized card crop."""
        try:
            raw_text = " ".join(self._recognize_windows_ocr(card).split())
        except Exception:
            return CatchObservation(True, status=RecognitionStatus.UNAVAILABLE, bounds=bounds)
        if not raw_text:
            return CatchObservation(True, status=RecognitionStatus.NO_TEXT, bounds=bounds)

        length_match = LENGTH_PATTERN.search(raw_text)
        length_inches = int(length_match.group(1)) if length_match else None
        result_type = ResultType.FISH if length_inches is not None else ResultType.ITEM
        title = raw_text[: length_match.start()].strip(" :-") if length_match else raw_text
        title = re.sub(
            r"\bLength\s*[.:;\]]?\s*$", "", title, flags=re.IGNORECASE
        ).strip()
        quantity_match = QUANTITY_PATTERN.search(title)
        quantity = int(quantity_match.group(1)) if quantity_match else 1
        if quantity_match:
            title = title[: quantity_match.start()].strip()
        catalog = FISH_NAMES if result_type is ResultType.FISH else ITEM_NAMES
        ranked = sorted(
            (
                SequenceMatcher(
                    None, self._normalize(title), self._normalize(candidate)
                ).ratio(),
                candidate,
            )
            for candidate in catalog
        )
        best_score, best_name = ranked[-1]
        second_score = ranked[-2][0] if len(ranked) > 1 else 0.0
        if best_score < 0.78 or (best_score < 1.0 and best_score - second_score < 0.06):
            return CatchObservation(
                True,
                length_inches=length_inches,
                quantity=max(1, quantity),
                confidence=best_score,
                status=RecognitionStatus.AMBIGUOUS,
                bounds=bounds,
            )
        return CatchObservation(
            True,
            result_type,
            best_name,
            length_inches,
            max(1, quantity),
            best_score,
            RecognitionStatus.RECOGNIZED,
            bounds,
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.casefold())

    @staticmethod
    def _recognize_windows_ocr(card: np.ndarray) -> str:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter

        rgba = cv2.cvtColor(card[:, :, :3], cv2.COLOR_BGR2RGBA)
        writer = DataWriter()
        writer.write_bytes(rgba.tobytes())
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.RGBA8, rgba.shape[1], rgba.shape[0]
        )
        language = Language("en-US")
        engine = OcrEngine.try_create_from_language(language)
        if engine is None:
            raise RuntimeError("Windows English OCR is unavailable")

        async def recognize() -> str:
            result = await engine.recognize_async(bitmap)
            return result.text

        return asyncio.run(recognize())
