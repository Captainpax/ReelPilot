import cv2
import numpy as np

from reelpilot.vision.bite import BiteDetector
from reelpilot.vision.cast_meter import CastMeterDetector
from reelpilot.vision.catch_card import CatchCardDetector, CatchResultReader
from reelpilot.vision.energy import EnergyMeterDetector, FoodPromptDetector
from reelpilot.vision.fishing_ui import FishingUiDetector
from reelpilot.vision.treasure import TreasureLootDetector


def energy_frame(fill_ratio: float) -> np.ndarray:
    """Build a template-free vertical energy rail at the supported baseline."""
    frame = np.zeros((759, 1296, 3), dtype=np.uint8)
    left, top, right, bottom = 1242, 438, 1284, 738
    cv2.rectangle(frame, (left, top), (right, bottom), (35, 95, 145), -1)
    cv2.rectangle(frame, (left + 10, top + 20), (right - 10, bottom - 18), (8, 8, 8), -1)
    inner_top = top + 24
    inner_bottom = bottom - 19
    fill_top = round(inner_bottom - (inner_bottom - inner_top) * fill_ratio)
    cv2.rectangle(
        frame,
        (left + 11, fill_top),
        (right - 11, inner_bottom),
        (30, 220, 80),
        -1,
    )
    return frame


def test_energy_meter_measures_bottom_contiguous_fill_boundaries() -> None:
    detector = EnergyMeterDetector()

    full = detector.detect(energy_frame(1.0))
    three_quarters = detector.detect(energy_frame(0.75))
    low = detector.detect(energy_frame(0.32))

    assert full.meter_detected and full.fill_ratio >= 0.95
    assert three_quarters.meter_detected
    assert 0.70 <= three_quarters.fill_ratio <= 0.80
    assert low.meter_detected
    assert 0.28 <= low.fill_ratio <= 0.36


def test_energy_meter_rejects_missing_or_detached_hud_fill() -> None:
    frame = energy_frame(0.0)
    frame[500:530, 1253:1273] = (30, 220, 80)
    observation = EnergyMeterDetector().detect(frame)

    assert observation.meter_detected
    assert observation.fill_ratio <= 0.03


def test_energy_meter_accepts_dim_nighttime_green_fill() -> None:
    frame = energy_frame(0.75)
    green = (frame[:, :, 1] > frame[:, :, 0]) & (
        frame[:, :, 1] > frame[:, :, 2]
    )
    frame[green] = (0, 64, 9)

    observation = EnergyMeterDetector().detect(frame)

    assert observation.meter_detected
    assert 0.70 <= observation.fill_ratio <= 0.80


def test_energy_meter_does_not_count_orange_empty_track_as_yellow_fill() -> None:
    frame = energy_frame(0.32)
    empty_track = frame[462:655, 1253:1273]
    black = np.all(empty_track == (8, 8, 8), axis=2)
    # HSV (20, 125, 255), matching the live orange empty rail at night.
    empty_track[black] = (130, 213, 255)

    observation = EnergyMeterDetector().detect(frame)

    assert observation.meter_detected
    assert 0.28 <= observation.fill_ratio <= 0.36


def test_food_prompt_requires_eating_text_and_located_yes(monkeypatch) -> None:
    frame = np.zeros((759, 1296, 3), dtype=np.uint8)
    cv2.rectangle(frame, (350, 250), (950, 450), (130, 180, 220), -1)
    detector = FoodPromptDetector()
    monkeypatch.setattr(
        detector,
        "_recognize_words",
        lambda _crop: (("Eat", (50, 30, 40, 20)), ("Yes", (420, 145, 60, 30))),
    )

    observation = detector.detect(frame)

    assert observation.prompt_detected
    assert observation.yes_center_pixels is not None


def test_food_prompt_never_guesses_when_yes_is_not_ocr_confirmed(monkeypatch) -> None:
    frame = np.zeros((759, 1296, 3), dtype=np.uint8)
    cv2.rectangle(frame, (350, 250), (950, 450), (130, 180, 220), -1)
    detector = FoodPromptDetector()
    monkeypatch.setattr(
        detector,
        "_recognize_words",
        lambda _crop: (("Eat", (50, 30, 40, 20)), ("No", (420, 145, 60, 30))),
    )

    assert not detector.detect(frame).prompt_detected


def test_bite_detector_accepts_stroke_and_dot_geometry() -> None:
    frame = np.zeros((400, 700, 3), dtype=np.uint8)
    yellow = (0, 210, 255)
    cv2.rectangle(frame, (200, 100), (204, 114), yellow, -1)
    cv2.rectangle(frame, (200, 118), (204, 122), yellow, -1)
    observation = BiteDetector().detect(frame)
    assert observation.bite_detected
    assert observation.confidence >= 0.6
    assert observation.bounds is not None
    assert observation.icon_center_pixels is not None


def test_bite_detector_rejects_missing_dot() -> None:
    frame = np.zeros((400, 700, 3), dtype=np.uint8)
    cv2.rectangle(frame, (200, 100), (204, 114), (0, 210, 255), -1)
    assert not BiteDetector().detect(frame).bite_detected


def cast_frame(fill_width: int) -> np.ndarray:
    frame = np.zeros((300, 500, 3), dtype=np.uint8)
    cv2.rectangle(frame, (180, 100), (320, 135), (40, 80, 120), -1)
    cv2.rectangle(frame, (183, 103), (317, 132), (210, 210, 210), -1)
    cv2.rectangle(frame, (188, 109), (188 + fill_width, 126), (20, 240, 80), -1)
    return frame


def test_cast_meter_tracks_charge_without_background_template() -> None:
    detector = CastMeterDetector()
    detector.begin(np.zeros((300, 500, 3), dtype=np.uint8))
    detector.observe(cast_frame(20))
    detector.observe(cast_frame(40))
    observation = detector.observe(cast_frame(60))
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


def test_cast_full_fill_normalizes_before_fixed_right_endpoint() -> None:
    detector = CastMeterDetector()
    observation = detector._measure(
        cast_frame(121),
        (180, 100, 321, 136),
    )

    assert observation.charge_ratio == 1.0
    assert observation.fill_width_pixels == observation.track_width_pixels


def test_cast_meter_acquires_dim_nighttime_fill() -> None:
    detector = CastMeterDetector()
    reference = np.zeros((300, 500, 3), dtype=np.uint8)
    detector.begin(reference)
    frame = np.zeros_like(reference)
    cv2.rectangle(frame, (180, 100), (320, 135), (8, 16, 24), -1)
    cv2.rectangle(frame, (183, 103), (317, 132), (12, 14, 18), -1)
    cv2.rectangle(frame, (188, 109), (230, 126), (5, 55, 20), -1)
    detector.observe(frame)
    cv2.rectangle(frame, (188, 109), (255, 126), (5, 55, 20), -1)
    detector.observe(frame)
    cv2.rectangle(frame, (188, 109), (280, 126), (5, 55, 20), -1)
    observation = detector.observe(frame)

    assert observation.meter_detected
    assert observation.charge_ratio > 0.5
    assert observation.tracking_confidence >= 0.55


def test_cast_meter_rejects_static_saturated_scene_rectangle() -> None:
    detector = CastMeterDetector()
    reference = np.zeros((300, 500, 3), dtype=np.uint8)
    detector.begin(reference)
    frame = np.zeros_like(reference)
    cv2.rectangle(frame, (180, 100), (315, 131), (25, 65, 110), -1)
    cv2.rectangle(frame, (184, 104), (214, 127), (70, 150, 220), -1)

    observations = [detector.observe(frame) for _ in range(15)]

    assert not any(observation.meter_detected for observation in observations)


def test_cast_meter_tracks_real_growth_while_static_candidate_is_present() -> None:
    detector = CastMeterDetector()
    reference = np.zeros((700, 1200, 3), dtype=np.uint8)
    detector.begin(reference)

    observations = []
    for fill_width in (18, 42, 70):
        frame = np.zeros_like(reference)
        # A plausible static scene rectangle must not monopolize acquisition.
        cv2.rectangle(frame, (180, 120), (320, 155), (40, 80, 120), -1)
        cv2.rectangle(frame, (183, 123), (317, 152), (210, 210, 210), -1)
        cv2.rectangle(frame, (188, 129), (255, 146), (20, 240, 80), -1)
        cv2.rectangle(frame, (420, 260), (560, 295), (40, 80, 120), -1)
        cv2.rectangle(frame, (423, 263), (557, 292), (210, 210, 210), -1)
        cv2.rectangle(
            frame,
            (428, 269),
            (428 + fill_width, 286),
            (20, 240, 80),
            -1,
        )
        observations.append(detector.observe(frame))

    assert observations[-1].meter_detected
    assert observations[-1].bounds is not None
    assert observations[-1].bounds[0] >= 400


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
    progress, confidence = FishingUiDetector._detect_progress(crop)
    assert 0.22 <= progress <= 0.24
    assert confidence >= 0.80


def test_progress_recognizes_green_full_channel() -> None:
    crop = np.zeros((471, 138, 3), dtype=np.uint8)
    crop[18:451, 108:119] = (0, 255, 0)

    progress, confidence = FishingUiDetector._detect_progress(crop)

    assert progress >= 0.99
    assert confidence >= 0.90


def test_progress_rejects_unanchored_colored_rows() -> None:
    crop = np.zeros((471, 138, 3), dtype=np.uint8)
    crop[100:180, 108:119] = (0, 255, 0)

    progress, confidence = FishingUiDetector._detect_progress(crop)

    assert progress == 0.0
    assert confidence >= 0.55


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


def test_impossible_one_frame_fish_jump_is_control_ineligible() -> None:
    detector = FishingUiDetector()

    def fish_crop(center_y_pixels: int) -> np.ndarray:
        crop = np.zeros((471, 138, 3), dtype=np.uint8)
        crop[260:356, 66:87] = (20, 220, 80)
        cv2.ellipse(
            crop,
            (76, center_y_pixels),
            (18, 10),
            0,
            0,
            360,
            (220, 220, 20),
            -1,
        )
        cv2.ellipse(
            crop,
            (76, center_y_pixels),
            (18, 10),
            0,
            0,
            360,
            (20, 40, 40),
            2,
        )
        return crop

    first = detector.analyze(fish_crop(300), 96)
    jumped = detector.analyze(fish_crop(361), 96)
    recovered = detector.analyze(fish_crop(302), 96)

    assert first.fish_confidence >= 0.55
    assert jumped.fish_confidence < 0.55
    assert recovered.fish_confidence >= 0.55


def test_sustained_new_fish_position_reacquires_after_three_frames() -> None:
    detector = FishingUiDetector()

    def fish_crop(center_y_pixels: int) -> np.ndarray:
        crop = np.zeros((471, 138, 3), dtype=np.uint8)
        crop[260:356, 66:87] = (20, 220, 80)
        cv2.ellipse(
            crop,
            (76, center_y_pixels),
            (18, 10),
            0,
            0,
            360,
            (220, 220, 20),
            -1,
        )
        cv2.ellipse(
            crop,
            (76, center_y_pixels),
            (18, 10),
            0,
            0,
            360,
            (20, 40, 40),
            2,
        )
        return crop

    initial = detector.analyze(fish_crop(300), 96)
    first = detector.analyze(fish_crop(360), 96)
    second = detector.analyze(fish_crop(361), 96)
    third = detector.analyze(fish_crop(362), 96)

    assert initial.fish_confidence >= 0.55
    assert first.fish_confidence < 0.55
    assert second.fish_confidence < 0.55
    assert third.fish_confidence >= 0.55


def test_containment_clips_fish_sprite_to_bottom_track_boundary() -> None:
    detector = FishingUiDetector()
    crop = np.zeros((471, 138, 3), dtype=np.uint8)
    crop[349:445, 66:87] = (20, 220, 80)
    cv2.ellipse(crop, (76, 440), (18, 10), 0, 0, 360, (220, 220, 20), -1)
    cv2.ellipse(crop, (76, 440), (18, 10), 0, 0, 360, (20, 40, 40), 2)

    observation = detector.analyze(crop, 96)

    assert observation.fish_bottom_y_pixels is not None
    assert observation.fish_bottom_y_pixels > detector.TRACK_BOTTOM_PIXELS
    assert observation.containment_margin_pixels == 0.0


def test_containment_includes_bottom_collision_cap_near_track_stop() -> None:
    detector = FishingUiDetector()
    crop = np.zeros((471, 138, 3), dtype=np.uint8)
    crop[341:433, 66:87] = (20, 220, 80)
    cv2.ellipse(crop, (76, 437), (18, 10), 0, 0, 360, (220, 220, 20), -1)
    cv2.ellipse(crop, (76, 437), (18, 10), 0, 0, 360, (20, 40, 40), 2)

    observation = detector.analyze(crop, 92)

    assert observation.bar_bottom_y_pixels == detector.TRACK_BOTTOM_PIXELS
    assert observation.containment_margin_pixels is not None
    assert observation.containment_margin_pixels >= 0.0


def test_treasure_is_detected_and_masked_before_fish_scoring() -> None:
    detector = FishingUiDetector()
    crop = np.zeros((471, 138, 3), dtype=np.uint8)
    crop[320:416, 66:87] = (20, 220, 80)
    cv2.rectangle(crop, (59, 180), (92, 212), (0, 100, 230), -1)
    cv2.rectangle(crop, (59, 180), (92, 212), (20, 20, 40), 3)
    cv2.ellipse(crop, (76, 365), (18, 10), 0, 0, 360, (220, 220, 20), -1)
    cv2.ellipse(crop, (76, 365), (18, 10), 0, 0, 360, (20, 40, 40), 2)

    observation = detector.analyze(crop, 96)

    assert observation.treasure_center_y_pixels is not None
    assert 190 <= observation.treasure_center_y_pixels <= 205
    assert observation.treasure_confidence >= 0.65
    assert observation.fish_center_y_pixels is not None
    assert 350 <= observation.fish_center_y_pixels <= 380


def test_treasure_loot_detector_finds_only_occupied_source_slots() -> None:
    frame = np.zeros((700, 1000, 3), dtype=np.uint8)

    def draw_row(y_pixels: int) -> None:
        for index in range(12):
            left = 130 + index * 58
            cv2.rectangle(frame, (left, y_pixels), (left + 51, y_pixels + 51), (30, 50, 70), -1)
            cv2.rectangle(
                frame,
                (left + 3, y_pixels + 3),
                (left + 48, y_pixels + 48),
                (180, 205, 225),
                -1,
            )

    for row_y in (100, 380, 438, 496):
        draw_row(row_y)
    cv2.circle(frame, (155, 125), 16, (20, 20, 20), -1)
    cv2.circle(frame, (155, 125), 12, (20, 210, 245), -1)

    observation = TreasureLootDetector().detect(frame)

    assert observation.menu_detected
    assert len(observation.source_slot_centers_pixels) == 12
    assert observation.occupied_slot_centers_pixels == ((155, 125),)
    assert observation.confidence >= 0.65


def test_treasure_loot_empty_night_slots_are_not_occupied() -> None:
    frame = np.zeros((700, 1000, 3), dtype=np.uint8)

    def draw_row(y_pixels: int) -> None:
        for index in range(12):
            left = 130 + index * 58
            cv2.rectangle(
                frame,
                (left, y_pixels),
                (left + 51, y_pixels + 51),
                (30, 50, 70),
                -1,
            )
            cv2.rectangle(
                frame,
                (left + 3, y_pixels + 3),
                (left + 48, y_pixels + 48),
                (130, 213, 255),
                -1,
            )

    for row_y in (100, 380, 438, 496):
        draw_row(row_y)
    cv2.circle(frame, (155, 125), 14, (20, 20, 20), -1)
    cv2.line(frame, (145, 115), (165, 135), (230, 230, 230), 3)

    observation = TreasureLootDetector().detect(frame)

    assert observation.menu_detected
    assert observation.occupied_slot_centers_pixels == ((155, 125),)
    assert observation.occupied_inventory_slot_count == 0


def test_treasure_loot_infers_backpack_grid_with_five_obscured_hotbar_slots() -> None:
    frame = np.zeros((700, 1000, 3), dtype=np.uint8)

    def draw_row(y_pixels: int) -> None:
        for index in range(12):
            left = 130 + index * 58
            cv2.rectangle(frame, (left, y_pixels), (left + 51, y_pixels + 51), (30, 50, 70), -1)
            cv2.rectangle(
                frame,
                (left + 3, y_pixels + 3),
                (left + 48, y_pixels + 48),
                (180, 205, 225),
                -1,
            )

    for row_y in (100, 158, 216, 380, 438, 496):
        draw_row(row_y)
    cv2.rectangle(frame, (845, 485), (915, 555), (30, 50, 70), -1)
    cv2.rectangle(frame, (850, 490), (910, 550), (80, 210, 230), -1)
    for index in range(5):
        left = 130 + index * 58
        cv2.rectangle(
            frame,
            (left + 1, 381),
            (left + 50, 430),
            (20 + index * 20, 80, 220),
            -1,
        )
        cv2.line(
            frame,
            (left + 5, 385),
            (left + 46, 426),
            (245, 245, 245),
            4,
        )

    observation = TreasureLootDetector().detect(frame)

    assert observation.menu_detected
    assert observation.inventory_slot_count == 36
    assert observation.occupied_inventory_slot_count >= 5
    assert observation.close_button_center_pixels is not None


def test_result_reader_recognizes_conservative_fish_name() -> None:
    reader = CatchResultReader()
    reader._recognize_windows_ocr = lambda _: "Bullhead Length: 31 in."  # type: ignore[method-assign]
    result = reader.read(np.zeros((100, 200, 3), np.uint8), (0, 0, 200, 100))
    assert result.name == "Bullhead"
    assert result.length_inches == 31
    assert result.result_type.value == "fish"


def test_result_reader_accepts_live_ocr_length_period() -> None:
    reader = CatchResultReader()
    reader._recognize_windows_ocr = lambda _: "Sardine Length. 13 in."  # type: ignore[method-assign]

    result = reader.read(np.zeros((100, 200, 3), np.uint8), (0, 0, 200, 100))

    assert result.name == "Sardine"
    assert result.length_inches == 13


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


def test_catch_detector_separates_white_card_from_adjacent_beige_scenery() -> None:
    frame = np.zeros((500, 900, 3), dtype=np.uint8)
    cv2.rectangle(frame, (390, 20), (700, 250), (120, 155, 210), -1)
    cv2.rectangle(frame, (330, 90), (570, 225), (225, 244, 254), -1)

    bounds = CatchCardDetector().locate(frame)

    assert bounds is not None
    left, top, right, bottom = bounds
    assert 325 <= left <= 340
    assert 85 <= top <= 100
    assert 560 <= right <= 580
    assert 215 <= bottom <= 235
