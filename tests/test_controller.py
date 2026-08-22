import pytest

from reelpilot.control import FishingController
from reelpilot.domain import ControllerProfile, FishingObservation


def observation(fish: float, bar: float, *, confidence: float = 1.0) -> FishingObservation:
    return FishingObservation(True, fish, bar, 96, 0.5, confidence, confidence)


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


def test_controller_slew_limits_each_cycle() -> None:
    controller = FishingController(ControllerProfile.NORMAL)
    first = controller.step(observation(400, 100), 96, 1.0)
    second = controller.step(observation(40, 400), 96, 1.02)
    assert abs(second - first) <= 0.25 + 1e-9


def test_controller_rejects_unreliable_observation() -> None:
    controller = FishingController()
    with pytest.raises(ValueError):
        controller.step(observation(200, 200, confidence=0.2), 96, 1.0)


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
