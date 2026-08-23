"""Adaptive predictive controller that keeps fish inside the reachable green bar."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..domain import ControllerProfile, ControlPhase, ControlTarget, FishingObservation
from .motion import MotionEstimator


@dataclass(frozen=True, slots=True)
class ControllerTuning:
    """Group profile-specific lookahead, gain, deadband, and slew parameters."""

    fish_lookahead_seconds: float
    bar_lookahead_seconds: float
    center_deadband_ratio: float
    velocity_deadband_ratio: float
    position_gain: float
    velocity_gain: float
    maximum_duty_step: float
    edge_margin_ratio: float


NORMAL_TUNING = ControllerTuning(
    0.06, 0.12, 0.05, 0.08, 0.60, 0.12, 0.25, 0.15
)
DARTING_TUNING = ControllerTuning(
    0.10, 0.14, 0.035, 0.04, 0.75, 0.18, 0.40, 0.10
)


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """Expose one fully explained controller decision for telemetry and replay."""

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
    edge_clearance_ratio: float
    center_correction_ratio: float
    containment_margin_pixels: float | None
    predicted_margin_pixels: float | None
    time_to_edge_seconds: float | None
    safety_override: bool
    control_phase: ControlPhase
    control_target: ControlTarget


class FishingController:
    """Predict fish and bar motion, then issue a bounded helper duty ratio.

    The controller targets the nearest physically reachable center. It clamps targets,
    learns a small per-minigame hover trim, and blends normal/darting profiles so
    classification changes cannot create an input step.
    """

    # These coordinates are in the normalized 138x471 fishing-UI crop and must
    # match ``FishingUiDetector``. The invariant is covered by a unit test so
    # feasible-target clamping cannot silently diverge from detector geometry.
    TRACK_TOP_PIXELS = 22.0
    TRACK_BOTTOM_PIXELS = 445.0
    BASE_HOVER_DUTY_RATIO = 0.78

    def __init__(self, profile: ControllerProfile = ControllerProfile.AUTO) -> None:
        """Create a controller using ``profile`` and empty motion history."""
        self.requested_profile = profile
        self._fish_motion = MotionEstimator()
        self._bar_motion = MotionEstimator()
        self.reset()

    def reset(self) -> None:
        """Reset filters, profile hysteresis, hover trim, and output slew state."""
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
        control_phase: ControlPhase = ControlPhase.PERFECT,
        target_y_pixels: float | None = None,
    ) -> float:
        """Return the next clamped duty ratio for one reliable observation."""
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
        control_target = (
            ControlTarget.TREASURE
            if target_y_pixels is not None
            else ControlTarget.FISH
        )
        requested_target_pixels = (
            target_y_pixels
            if target_y_pixels is not None
            else feasible_fish_pixels + fish_delta_pixels
        )
        feasible_target_pixels = min(
            legal_bottom_pixels,
            max(legal_top_pixels, requested_target_pixels),
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
        predicted_fish_pixels = min(
            self.TRACK_BOTTOM_PIXELS,
            max(
                self.TRACK_TOP_PIXELS,
                fish.position_pixels + fish_delta_pixels,
            ),
        )
        fish_half_height_pixels = 11.0
        if (
            observation.fish_top_y_pixels is not None
            and observation.fish_bottom_y_pixels is not None
        ):
            fish_half_height_pixels = max(
                7.0,
                min(
                    16.0,
                    (observation.fish_bottom_y_pixels - observation.fish_top_y_pixels)
                    / 2.0,
                ),
            )
        predicted_fish_top = max(
            self.TRACK_TOP_PIXELS,
            predicted_fish_pixels - fish_half_height_pixels,
        )
        predicted_fish_bottom = min(
            self.TRACK_BOTTOM_PIXELS,
            predicted_fish_pixels + fish_half_height_pixels,
        )
        predicted_bar_top = predicted_bar_pixels - half_bar_pixels
        predicted_bar_bottom = predicted_bar_pixels + half_bar_pixels
        upper_margin_pixels = predicted_fish_top - predicted_bar_top
        lower_margin_pixels = predicted_bar_bottom - predicted_fish_bottom
        predicted_margin_pixels = min(upper_margin_pixels, lower_margin_pixels)
        actual_margin_pixels = observation.containment_margin_pixels
        relative_velocity_pixels_per_second = (
            fish.velocity_pixels_per_second - bar.velocity_pixels_per_second
        )
        closing_speed = (
            -relative_velocity_pixels_per_second
            if upper_margin_pixels <= lower_margin_pixels
            else relative_velocity_pixels_per_second
        )
        time_to_edge_seconds = (
            max(0.0, predicted_margin_pixels) / closing_speed
            if closing_speed > 1e-6
            else None
        )
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

        perfect_center_hold = (
            control_phase is ControlPhase.PERFECT
            and control_target is ControlTarget.FISH
            and actual_margin_pixels is not None
            and actual_margin_pixels >= bar_length_pixels * 0.20
            and abs(raw_error_pixels) <= bar_length_pixels * 0.15
            and abs(bar.velocity_pixels_per_second)
            <= bar_length_pixels * 0.50
        )
        if perfect_center_hold:
            # Do not chase every small fish movement while it is already well
            # contained.  Stardew's bar has substantial inertia; accelerating it
            # inside the safe interior sacrifices the reserve needed when a fish
            # abruptly reverses.  The guarded-margin correction below can still
            # break this hover immediately when prediction identifies real danger.
            normalized_error = 0.0
            relative_velocity_ratio = 0.0

        guarded_margin_ratio = 0.16 if self._active_profile is ControllerProfile.DARTING else 0.12
        if control_phase is ControlPhase.PERFECT and predicted_margin_pixels < (
            guarded_margin_ratio * bar_length_pixels
        ):
            guard_gap_ratio = (
                guarded_margin_ratio * bar_length_pixels - predicted_margin_pixels
            ) / max(1.0, bar_length_pixels)
            normalized_error += (
                -guard_gap_ratio if upper_margin_pixels <= lower_margin_pixels else guard_gap_ratio
            )

        absolute_error_ratio = abs(normalized_error)
        edge_clearance_ratio = max(0.0, 0.5 - absolute_error_ratio)
        # Use the complete feasible error outside the deadband. The earlier
        # containment-first curve deliberately removed most of the first 6-10% of
        # center error. Live telemetry showed that this left the fish consistently
        # riding the top edge even though it remained technically contained.
        center_correction_ratio = normalized_error
        velocity_correction_ratio = relative_velocity_ratio
        if control_phase is ControlPhase.PERFECT and control_target is ControlTarget.FISH:
            # Prediction is essential for braking an inertial bar, but it can briefly
            # report a centered future state while the visible bar is still several
            # pixels behind the fish.  A small bounded contribution from the current
            # center error keeps real interior reserve on both sides of the fish.  It
            # is deliberately Perfect-only: recovery retains the replay-proven
            # predictive response and treasure targeting is not biased toward fish.
            actual_center_bias_ratio = max(
                -0.08,
                min(0.08, 0.45 * raw_error_pixels / max(1.0, bar_length_pixels)),
            )
            center_correction_ratio += actual_center_bias_ratio
        if edge_clearance_ratio <= tuning.edge_margin_ratio:
            # Near an edge, preserve the full predicted error and brake
            # relative momentum earlier than center-only control would.
            center_correction_ratio = normalized_error
            velocity_correction_ratio *= 1.35

        effective_hover = self.BASE_HOVER_DUTY_RATIO + self._hover_trim_ratio
        unslewed_duty = (
            effective_hover
            - tuning.position_gain * center_correction_ratio
            - tuning.velocity_gain * velocity_correction_ratio
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
            control_target,
        ):
            self._hover_trim_ratio -= 0.60 * normalized_error * elapsed_seconds
            self._hover_trim_ratio = max(-0.12, min(0.12, self._hover_trim_ratio))

        lower_boundary_anchor = (
            control_phase is ControlPhase.PERFECT
            and predicted_fish_pixels
            >= legal_bottom_pixels + guarded_margin_ratio * bar_length_pixels
            and bar.position_pixels >= legal_bottom_pixels - bar_length_pixels * 0.08
        )
        upper_boundary_anchor = (
            control_phase is ControlPhase.PERFECT
            and predicted_fish_pixels
            <= legal_top_pixels - guarded_margin_ratio * bar_length_pixels
            and bar.position_pixels <= legal_top_pixels + bar_length_pixels * 0.08
        )
        safety_override = (
            control_phase is ControlPhase.PERFECT
            and (
                predicted_margin_pixels < bar_length_pixels * 0.06
                or lower_boundary_anchor
                or upper_boundary_anchor
            )
        )
        if upper_boundary_anchor:
            duty_ratio = 1.0
        elif lower_boundary_anchor:
            duty_ratio = 0.0
        elif safety_override and upper_margin_pixels <= lower_margin_pixels:
            duty_ratio = 1.0
        elif safety_override:
            bottom_landing = (
                predicted_bar_pixels >= legal_bottom_pixels - bar_length_pixels * 0.08
                and bar.velocity_pixels_per_second > 0.0
            )
            duty_ratio = 0.35 if bottom_landing else 0.0
        else:
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
            predicted_fish_pixels,
            predicted_bar_pixels,
            feasible_target_pixels,
            raw_error_pixels,
            feasible_error_pixels,
            self._hover_trim_ratio,
            effective_hover,
            edge_clearance_ratio,
            center_correction_ratio,
            actual_margin_pixels,
            predicted_margin_pixels,
            time_to_edge_seconds,
            safety_override,
            control_phase,
            control_target,
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
        control_target: ControlTarget,
    ) -> bool:
        boundary_margin = bar_length_pixels * 0.10
        return (
            control_target is ControlTarget.FISH
            and self._active_profile is ControllerProfile.NORMAL
            and observation.fish_confidence >= 0.70
            and observation.bar_confidence >= 0.70
            and legal_top_pixels + boundary_margin
            <= fish_position_pixels
            <= legal_bottom_pixels - boundary_margin
            and abs(fish_velocity) < bar_length_pixels * 0.35
            and abs(bar_velocity) < bar_length_pixels * 0.50
            and 0.05 < unslewed_duty < 0.95
        )
