import pytest

from reelpilot.control import FishingController
from reelpilot.domain import ControllerProfile, ControlPhase, FishingObservation
from reelpilot.vision.fishing_ui import FishingUiDetector


def observation(fish: float, bar: float, *, confidence: float = 1.0) -> FishingObservation:
    return FishingObservation(True, fish, bar, 96, 0.5, confidence, confidence)


def test_controller_and_detector_share_track_coordinates() -> None:
    assert FishingController.TRACK_TOP_PIXELS == FishingUiDetector.TRACK_TOP_PIXELS
    assert FishingController.TRACK_BOTTOM_PIXELS == FishingUiDetector.TRACK_BOTTOM_PIXELS


def test_controller_moves_up_harder_when_fish_is_above_bar() -> None:
    controller = FishingController(ControllerProfile.NORMAL)
    duty = controller.step(observation(100, 200), 96, 1.0)
    assert duty > 0.5
    assert 0.0 <= duty <= 1.0


def test_controller_reduces_duty_when_fish_is_below_bar() -> None:
    controller = FishingController(ControllerProfile.NORMAL)
    controller.step(observation(200, 200), 96, 1.0)
    duty = controller.step(observation(300, 200), 96, 1.02)
    assert duty < controller.last_decision.effective_hover_duty_ratio  # type: ignore[union-attr]


def test_darting_profile_closes_a_large_center_gap_more_aggressively() -> None:
    normal = FishingController(ControllerProfile.NORMAL)
    darting = FishingController(ControllerProfile.DARTING)
    for index in range(6):
        timestamp = 1.0 + index * 0.02
        normal_duty = normal.step(observation(180, 200), 96, timestamp)
        darting_duty = darting.step(observation(180, 200), 96, timestamp)

    assert darting_duty > normal_duty


def test_controller_slew_limits_each_cycle() -> None:
    controller = FishingController(ControllerProfile.NORMAL)
    first = controller.step(observation(400, 100), 96, 1.0, ControlPhase.RECOVERY)
    second = controller.step(observation(40, 400), 96, 1.02, ControlPhase.RECOVERY)
    assert abs(second - first) <= 0.25 + 1e-9


def test_controller_rejects_unreliable_observation() -> None:
    controller = FishingController()
    with pytest.raises(ValueError):
        controller.step(observation(200, 200, confidence=0.2), 96, 1.0)


def test_controller_uses_the_full_feasible_center_error() -> None:
    inside = FishingController(ControllerProfile.NORMAL)
    outside = FishingController(ControllerProfile.NORMAL)

    inside.step(observation(205, 200), 96, 1.0, ControlPhase.RECOVERY)
    outside.step(observation(240, 200), 96, 1.0, ControlPhase.RECOVERY)

    assert inside.last_decision is not None
    assert outside.last_decision is not None
    assert inside.last_decision.center_correction_ratio == pytest.approx(5 / 96)
    assert outside.last_decision.center_correction_ratio == pytest.approx(40 / 96)
    assert abs(inside.last_decision.center_correction_ratio) < abs(
        outside.last_decision.center_correction_ratio
    )
    assert outside.last_decision.edge_clearance_ratio < inside.last_decision.edge_clearance_ratio


def test_perfect_phase_corrects_visible_center_lag_without_changing_recovery() -> None:
    perfect = FishingController(ControllerProfile.NORMAL)
    recovery = FishingController(ControllerProfile.NORMAL)

    perfect.step(observation(190, 200), 96, 1.0, ControlPhase.PERFECT)
    recovery.step(observation(190, 200), 96, 1.0, ControlPhase.RECOVERY)

    assert perfect.last_decision is not None
    assert recovery.last_decision is not None
    assert perfect.last_decision.center_correction_ratio == pytest.approx(
        recovery.last_decision.center_correction_ratio + 0.45 * (-10 / 96)
    )


def test_perfect_visible_center_correction_is_bounded() -> None:
    controller = FishingController(ControllerProfile.NORMAL)
    controller.step(observation(180, 200), 96, 1.0, ControlPhase.PERFECT)

    assert controller.last_decision is not None
    predicted_only = controller.last_decision.feasible_error_pixels / 96
    assert controller.last_decision.center_correction_ratio == pytest.approx(
        predicted_only - 0.08
    )


def test_perfect_phase_does_not_chase_motion_inside_safe_center_corridor() -> None:
    perfect = FishingController(ControllerProfile.NORMAL)
    recovery = FishingController(ControllerProfile.NORMAL)
    centered = FishingObservation(
        True,
        212,
        200,
        96,
        fish_confidence=1.0,
        bar_confidence=1.0,
        fish_top_y_pixels=201,
        fish_bottom_y_pixels=223,
        bar_top_y_pixels=152,
        bar_bottom_y_pixels=248,
        containment_margin_pixels=25,
    )

    perfect.step(centered, 96, 1.0, ControlPhase.PERFECT)
    recovery.step(centered, 96, 1.0, ControlPhase.RECOVERY)

    assert perfect.last_decision is not None
    assert recovery.last_decision is not None
    assert perfect.last_decision.center_correction_ratio == pytest.approx(
        0.45 * 12 / 96
    )
    assert recovery.last_decision.center_correction_ratio == pytest.approx(12 / 96)


def test_auto_profile_enters_darting_after_sustained_motion() -> None:
    controller = FishingController(ControllerProfile.AUTO)
    for index, fish in enumerate((100, 105, 115, 130, 150, 175)):
        controller.step(observation(fish, 200), 72, 1.0 + index * 0.02)
    assert controller.last_decision is not None
    assert controller.last_decision.active_profile is ControllerProfile.DARTING


def test_feasible_target_is_inside_legal_center_range() -> None:
    controller = FishingController(ControllerProfile.NORMAL)
    controller.step(observation(440, 300), 96, 1.0)
    decision = controller.last_decision
    assert decision is not None
    assert 70 <= decision.feasible_target_pixels <= 397


def test_perfect_phase_overrides_slew_for_immediate_upper_edge_protection() -> None:
    controller = FishingController(ControllerProfile.NORMAL)
    edge_observation = FishingObservation(
        True,
        97,
        130,
        96,
        fish_confidence=1.0,
        bar_confidence=1.0,
        fish_top_y_pixels=86,
        fish_bottom_y_pixels=108,
        bar_top_y_pixels=82,
        bar_bottom_y_pixels=178,
        containment_margin_pixels=4,
    )

    duty = controller.step(edge_observation, 96, 1.0, ControlPhase.PERFECT)

    assert duty == 1.0
    assert controller.last_decision is not None
    assert controller.last_decision.safety_override
    assert controller.last_decision.predicted_margin_pixels is not None


def test_perfect_phase_anchors_bar_at_bottom_boundary() -> None:
    controller = FishingController(ControllerProfile.NORMAL)
    bottom_observation = FishingObservation(
        True,
        418,
        395,
        96,
        fish_confidence=1.0,
        bar_confidence=1.0,
        fish_top_y_pixels=407,
        fish_bottom_y_pixels=429,
        bar_top_y_pixels=347,
        bar_bottom_y_pixels=443,
        containment_margin_pixels=14,
    )

    duty = controller.step(bottom_observation, 96, 1.0, ControlPhase.PERFECT)

    assert duty == 0.0
    assert controller.last_decision is not None
    assert controller.last_decision.safety_override
    assert controller.last_decision.predicted_fish_pixels == pytest.approx(418)
    assert controller.last_decision.predicted_margin_pixels == pytest.approx(14)
