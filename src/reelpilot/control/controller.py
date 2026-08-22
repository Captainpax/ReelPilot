from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..domain import ControllerProfile, FishingObservation
from .motion import MotionEstimator


@dataclass(frozen=True, slots=True)
class ControllerTuning:
    fish_lookahead_seconds: float
    bar_lookahead_seconds: float
    center_deadband_ratio: float
    velocity_deadband_ratio: float
    position_gain: float
    velocity_gain: float
    maximum_duty_step: float


NORMAL_TUNING = ControllerTuning(0.06, 0.12, 0.05, 0.08, 0.45, 0.12, 0.25)
DARTING_TUNING = ControllerTuning(0.10, 0.14, 0.035, 0.04, 0.60, 0.18, 0.40)


@dataclass(frozen=True, slots=True)
class ControlDecision:
    duty_ratio: float
    active_profile: ControllerProfile
    profile_blend_ratio: float
    fish_position_pixels: float
    bar_position_pixels: float
    fish_velocity_pixels_per_second: float
    bar_velocity_pixels_per_second: float
    predicted_fish_pixels: float
    predicted_bar_pixels: float
    feasible_target_pixels: float
    raw_center_error_pixels: float
    feasible_error_pixels: float
    hover_trim_ratio: float
    effective_hover_duty_ratio: float


class FishingController:
    TRACK_TOP_PIXELS = 22.0
    TRACK_BOTTOM_PIXELS = 445.0
    BASE_HOVER_DUTY_RATIO = 0.78

    def __init__(self, profile: ControllerProfile = ControllerProfile.AUTO) -> None:
        self.requested_profile = profile
        self._fish_motion = MotionEstimator()
        self._bar_motion = MotionEstimator()
        self.reset()

    def reset(self) -> None:
        self._fish_motion.reset()
        self._bar_motion.reset()
        self._last_timestamp_seconds: float | None = None
        self._last_duty_ratio = 0.5
        self._hover_trim_ratio = 0.0
        self._profile_blend_ratio = (
            1.0 if self.requested_profile is ControllerProfile.DARTING else 0.0
        )
        self._speed_ratios: deque[float] = deque(maxlen=5)
        self._calm_since_seconds: float | None = None
        self._active_profile = (
            ControllerProfile.NORMAL
            if self.requested_profile is ControllerProfile.AUTO
            else self.requested_profile
        )
        self.last_decision: ControlDecision | None = None

    def step(
        self,
        observation: FishingObservation,
        bar_length_pixels: int,
        timestamp_seconds: float,
    ) -> float:
        if not observation.control_ready:
            raise ValueError("controller requires reliable fish and bar observations")
        assert observation.fish_center_y_pixels is not None
        assert observation.bar_center_y_pixels is not None
        elapsed_seconds = (
            0.02
            if self._last_timestamp_seconds is None
            else max(0.01, min(0.1, timestamp_seconds - self._last_timestamp_seconds))
        )
        self._last_timestamp_seconds = timestamp_seconds

        fish = self._fish_motion.update(
            observation.fish_center_y_pixels,
            timestamp_seconds,
            observation.fish_confidence,
            bar_length_pixels,
        )
        bar = self._bar_motion.update(
            observation.bar_center_y_pixels,
            timestamp_seconds,
            observation.bar_confidence,
            bar_length_pixels,
        )
        self._update_profile(
            abs(fish.velocity_pixels_per_second) / bar_length_pixels, timestamp_seconds
        )
        self._blend_profile(elapsed_seconds)
        tuning = self._interpolated_tuning()

        half_bar_pixels = bar_length_pixels / 2.0
        legal_top_pixels = self.TRACK_TOP_PIXELS + half_bar_pixels
        legal_bottom_pixels = self.TRACK_BOTTOM_PIXELS - half_bar_pixels
        feasible_fish_pixels = min(
            legal_bottom_pixels, max(legal_top_pixels, fish.position_pixels)
        )
        prediction_limit_pixels = bar_length_pixels * 0.35
        fish_delta_pixels = max(
            -prediction_limit_pixels,
            min(
                prediction_limit_pixels,
                fish.velocity_pixels_per_second * tuning.fish_lookahead_seconds,
            ),
        )
        feasible_target_pixels = min(
            legal_bottom_pixels,
            max(legal_top_pixels, feasible_fish_pixels + fish_delta_pixels),
        )
        predicted_bar_pixels = min(
            legal_bottom_pixels,
            max(
                legal_top_pixels,
                bar.position_pixels
                + bar.velocity_pixels_per_second * tuning.bar_lookahead_seconds,
            ),
        )
        raw_error_pixels = fish.position_pixels - bar.position_pixels
        feasible_error_pixels = feasible_target_pixels - predicted_bar_pixels
        normalized_error = feasible_error_pixels / max(1.0, bar_length_pixels)
        relative_velocity_ratio = (
            fish.velocity_pixels_per_second - bar.velocity_pixels_per_second
        ) / max(1.0, bar_length_pixels)

        if (
            abs(normalized_error) <= tuning.center_deadband_ratio
            and abs(relative_velocity_ratio) <= tuning.velocity_deadband_ratio
        ):
            normalized_error = 0.0
            relative_velocity_ratio = 0.0

        effective_hover = self.BASE_HOVER_DUTY_RATIO + self._hover_trim_ratio
        unslewed_duty = (
            effective_hover
            - tuning.position_gain * normalized_error
            - tuning.velocity_gain * relative_velocity_ratio
        )
        unslewed_duty = max(0.0, min(1.0, unslewed_duty))
        if self._can_learn_trim(
            observation,
            fish.position_pixels,
            fish.velocity_pixels_per_second,
            bar.velocity_pixels_per_second,
            legal_top_pixels,
            legal_bottom_pixels,
            bar_length_pixels,
            unslewed_duty,
        ):
            self._hover_trim_ratio -= 0.60 * normalized_error * elapsed_seconds
            self._hover_trim_ratio = max(-0.12, min(0.12, self._hover_trim_ratio))

        lower = self._last_duty_ratio - tuning.maximum_duty_step
        upper = self._last_duty_ratio + tuning.maximum_duty_step
        duty_ratio = max(0.0, min(1.0, max(lower, min(upper, unslewed_duty))))
        self._last_duty_ratio = duty_ratio
        self.last_decision = ControlDecision(
            duty_ratio,
            self._active_profile,
            self._profile_blend_ratio,
            fish.position_pixels,
            bar.position_pixels,
            fish.velocity_pixels_per_second,
            bar.velocity_pixels_per_second,
            feasible_target_pixels,
            predicted_bar_pixels,
            feasible_target_pixels,
            raw_error_pixels,
            feasible_error_pixels,
            self._hover_trim_ratio,
            effective_hover,
        )
        return duty_ratio

    def _update_profile(self, speed_ratio: float, timestamp_seconds: float) -> None:
        if self.requested_profile is not ControllerProfile.AUTO:
            self._active_profile = self.requested_profile
            return
        self._speed_ratios.append(speed_ratio)
        fast_count = sum(value >= 0.65 for value in self._speed_ratios)
        if len(self._speed_ratios) >= 4 and fast_count >= 3:
            self._active_profile = ControllerProfile.DARTING
            self._calm_since_seconds = None
            return
        calm = sum(value > 0.45 for value in self._speed_ratios) < 2
        if self._active_profile is ControllerProfile.DARTING and calm:
            self._calm_since_seconds = self._calm_since_seconds or timestamp_seconds
            if timestamp_seconds - self._calm_since_seconds >= 0.35:
                self._active_profile = ControllerProfile.NORMAL
        elif not calm:
            self._calm_since_seconds = None

    def _blend_profile(self, elapsed_seconds: float) -> None:
        target = 1.0 if self._active_profile is ControllerProfile.DARTING else 0.0
        duration = 0.08 if target > self._profile_blend_ratio else 0.16
        step = elapsed_seconds / duration
        if target > self._profile_blend_ratio:
            self._profile_blend_ratio = min(target, self._profile_blend_ratio + step)
        else:
            self._profile_blend_ratio = max(target, self._profile_blend_ratio - step)

    def _interpolated_tuning(self) -> ControllerTuning:
        ratio = self._profile_blend_ratio
        values = []
        for normal, darting in zip(
            NORMAL_TUNING.__match_args__, DARTING_TUNING.__match_args__, strict=True
        ):
            values.append(
                getattr(NORMAL_TUNING, normal) * (1.0 - ratio)
                + getattr(DARTING_TUNING, darting) * ratio
            )
        return ControllerTuning(*values)

    def _can_learn_trim(
        self,
        observation: FishingObservation,
        fish_position_pixels: float,
        fish_velocity: float,
        bar_velocity: float,
        legal_top_pixels: float,
        legal_bottom_pixels: float,
        bar_length_pixels: int,
        unslewed_duty: float,
    ) -> bool:
        boundary_margin = bar_length_pixels * 0.10
        return (
            self._active_profile is ControllerProfile.NORMAL
            and observation.fish_confidence >= 0.70
            and observation.bar_confidence >= 0.70
            and legal_top_pixels + boundary_margin
            <= fish_position_pixels
            <= legal_bottom_pixels - boundary_margin
            and abs(fish_velocity) < bar_length_pixels * 0.35
            and abs(bar_velocity) < bar_length_pixels * 0.50
            and 0.05 < unslewed_duty < 0.95
        )
