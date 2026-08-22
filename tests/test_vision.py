import cv2
import numpy as np

from reelpilot.vision.bite import BiteDetector
from reelpilot.vision.cast_meter import CastMeterDetector
from reelpilot.vision.catch_card import CatchCardDetector, CatchResultReader
from reelpilot.vision.fishing_ui import FishingUiDetector


def test_bite_detector_accepts_stroke_and_dot_geometry() -> None:
    frame = np.zeros((400, 700, 3), dtype=np.uint8)
    yellow = (0, 210, 255)
    cv2.rectangle(frame, (200, 100), (204, 114), yellow, -1)
    cv2.rectangle(frame, (200, 118), (204, 122), yellow, -1)
    detected, confidence = BiteDetector().detect(frame)
    assert detected
    assert confidence >= 0.6


def test_bite_detector_rejects_missing_dot() -> None:
    frame = np.zeros((400, 700, 3), dtype=np.uint8)
    cv2.rectangle(frame, (200, 100), (204, 114), (0, 210, 255), -1)
    assert not BiteDetector().detect(frame)[0]


def cast_frame(fill_width: int) -> np.ndarray:
    frame = np.zeros((300, 500, 3), dtype=np.uint8)
    cv2.rectangle(frame, (180, 100), (320, 135), (40, 80, 120), -1)
    cv2.rectangle(frame, (183, 103), (317, 132), (210, 210, 210), -1)
    cv2.rectangle(frame, (188, 109), (188 + fill_width, 126), (20, 240, 80), -1)
    return frame


def test_cast_meter_tracks_charge_without_background_template() -> None:
    detector = CastMeterDetector()
    detector.begin(np.zeros((300, 500, 3), dtype=np.uint8))
    rising = cast_frame(60)
    detector.observe(rising)
    observation = detector.observe(rising)
    assert observation.meter_detected
    assert observation.bounds is not None
    assert observation.charge_ratio > 0.25
    assert observation.fill_width_pixels is not None
    assert observation.track_width_pixels is not None


def test_cast_measurement_bridges_one_dark_fill_column() -> None:
    detector = CastMeterDetector()
    frame = cast_frame(110)
    frame[109:127, 230] = 0
    observation = detector._measure(frame, (180, 100, 321, 136))
    assert observation.meter_detected
    assert observation.charge_ratio > 0.7


def test_cast_meter_acquires_dim_nighttime_fill() -> None:
    detector = CastMeterDetector()
    reference = np.zeros((300, 500, 3), dtype=np.uint8)
    detector.begin(reference)
    frame = np.zeros_like(reference)
    cv2.rectangle(frame, (180, 100), (320, 135), (8, 16, 24), -1)
    cv2.rectangle(frame, (183, 103), (317, 132), (12, 14, 18), -1)
    cv2.rectangle(frame, (188, 109), (280, 126), (5, 55, 20), -1)

    detector.observe(frame)
    observation = detector.observe(frame)

    assert observation.meter_detected
    assert observation.charge_ratio > 0.5
    assert observation.tracking_confidence >= 0.55


def test_fishing_bar_is_measured_from_green_row_evidence() -> None:
    detector = FishingUiDetector()
    crop = np.zeros((471, 138, 4), dtype=np.uint8)
    crop[180:276, 66:87, :3] = (20, 220, 80)
    center, length, confidence = detector._detect_bar(crop, None)
    assert center is not None
    assert length is not None and 90 <= length <= 100
    assert confidence >= 0.55


def test_short_green_fish_run_does_not_extend_bar_calibration() -> None:
    detector = FishingUiDetector()
    crop = np.zeros((471, 138, 4), dtype=np.uint8)
    crop[250:346, 66:87, :3] = (20, 220, 80)
    crop[229:249, 66:87, :3] = (20, 220, 80)
    center, length, confidence = detector._detect_bar(crop, None)
    assert center is not None
    assert length == 96
    assert confidence >= 0.55


def test_fishing_ui_locator_recovers_from_tall_panel_rails() -> None:
    detector = FishingUiDetector()
    frame = np.zeros((759, 1296, 3), dtype=np.uint8)
    left, top = 463, 114
    cv2.rectangle(frame, (left, top), (left + 137, top + 470), (90, 110, 125), -1)
    for offset in (1, 25, 61, 85, 106, 124, 133):
        cv2.line(
            frame,
            (left + offset, top + 20),
            (left + offset, top + 450),
            (235, 235, 235),
            2,
        )
    frame[top + 180 : top + 276, left + 66 : left + 87] = (20, 220, 80)
    bounds = detector.locate(frame)
    assert bounds is not None
    assert abs(bounds.left_pixels - left) <= 4
    assert abs(bounds.top_pixels - top) <= 8


def test_progress_uses_gold_fill_instead_of_bright_rails() -> None:
    crop = np.zeros((471, 138, 3), dtype=np.uint8)
    crop[18:451, 108:119] = (67, 142, 203)
    crop[351:451, 108:119] = (0, 190, 255)
    progress = FishingUiDetector._detect_progress(crop)
    assert 0.22 <= progress <= 0.24


def test_progress_recognizes_green_full_channel() -> None:
    crop = np.zeros((471, 138, 3), dtype=np.uint8)
    crop[18:451, 108:119] = (0, 255, 0)

    progress = FishingUiDetector._detect_progress(crop)

    assert progress >= 0.99


def test_stale_roi_without_green_bar_is_invalid() -> None:
    detector = FishingUiDetector()
    crop = np.zeros((471, 138, 3), dtype=np.uint8)
    for x in (55, 66, 87, 98):
        cv2.line(crop, (x, 22), (x, 444), (220, 220, 220), 2)
    assert not detector.is_valid_crop(crop)


def test_fish_color_scoring_rejects_orange_treasure_chest() -> None:
    detector = FishingUiDetector()
    crop = np.zeros((471, 138, 3), dtype=np.uint8)
    crop[320:416, 66:87] = (20, 220, 80)
    cv2.rectangle(crop, (59, 180), (92, 212), (0, 100, 230), -1)
    cv2.rectangle(crop, (59, 180), (92, 212), (20, 20, 40), 3)
    cv2.ellipse(crop, (76, 365), (18, 10), 0, 0, 360, (220, 220, 20), -1)
    cv2.ellipse(crop, (76, 365), (18, 10), 0, 0, 360, (20, 40, 40), 2)
    center, confidence = detector._detect_fish(crop, 368.0)
    assert center is not None
    assert 350 <= center <= 380
    assert confidence >= 0.55


def test_result_reader_recognizes_conservative_fish_name() -> None:
    reader = CatchResultReader()
    reader._recognize_windows_ocr = lambda _: "Bullhead Length: 31 in."  # type: ignore[method-assign]
    result = reader.read(np.zeros((100, 200, 3), np.uint8), (0, 0, 200, 100))
    assert result.name == "Bullhead"
    assert result.length_inches == 31
    assert result.result_type.value == "fish"


def test_result_reader_does_not_guess_ambiguous_name() -> None:
    reader = CatchResultReader()
    reader._recognize_windows_ocr = lambda _: "Something Length: 12 in."  # type: ignore[method-assign]
    result = reader.read(np.zeros((100, 200, 3), np.uint8), (0, 0, 200, 100))
    assert result.name is None
    assert result.result_type.value == "unknown"


def test_catch_detector_locates_white_instant_item_bubble() -> None:
    frame = np.zeros((500, 900, 3), dtype=np.uint8)
    cv2.rectangle(frame, (330, 90), (570, 225), (235, 245, 250), -1)
    cv2.rectangle(frame, (330, 90), (570, 225), (90, 120, 145), 3)
    bounds = CatchCardDetector().locate(frame)
    assert bounds is not None
    left, top, right, bottom = bounds
    assert left <= 335 and top <= 95
    assert right >= 565 and bottom >= 220
