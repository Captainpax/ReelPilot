"""Deterministic state machine for casting, hooking, catching, and safe recovery."""

from __future__ import annotations

import sqlite3
import statistics
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter

from ..control import FishingController
from ..domain import (
    AutomationMode,
    AutomationState,
    BiteObservation,
    CastObservation,
    CastReleaseMethod,
    CatchObservation,
    ControlPhase,
    DashboardView,
    EncounterContext,
    EncounterOutcome,
    EnergyObservation,
    EnergyStatus,
    FishingObservation,
    FoodPromptObservation,
    MaxVerification,
    PerfectStatus,
    ReelPilotSettings,
    ResultType,
    RuntimeSnapshot,
    StartMode,
    TreasureLootObservation,
    TreasureStatus,
)
from ..protocols import DashboardPort, InputPort, VisionPort
from ..recording import SessionRecorder
from ..stats import CatchRecord, HistoricalStatsSnapshot, StatsService, StatsSnapshot
from .calibration import BarCalibrator


class EngineResult(StrEnum):
    """Describe why the automation loop returned to the application."""

    STOPPED = "stopped"
    BITE_TIMEOUT = "bite-timeout"
    INVENTORY_FULL = "inventory-full"
    FOOD_UNAVAILABLE = "food-unavailable"
    ENERGY_UNREADABLE = "energy-unreadable"


class _StopRequested(Exception):
    pass


class _StateRedirect(Exception):
    def __init__(self, state: AutomationState) -> None:
        self.state = state


class AutomationEngine:
    """Drive one live automation session without owning platform resources."""

    CONTROL_INTERVAL_SECONDS = 0.020
    BITE_TIMEOUT_SECONDS = 45.0

    def __init__(
        self,
        settings: ReelPilotSettings,
        vision: VisionPort,
        dashboard: DashboardPort,
        *,
        stats: StatsService | None = None,
        recorder: SessionRecorder | None = None,
        start_mode: StartMode = StartMode.NORMAL,
        stop_requested: Callable[[], bool],
        pause_requested: Callable[[], bool],
        debug_requested: Callable[[], bool] = lambda: False,
        stats_requested: Callable[[], bool] = lambda: False,
        previous_stats_page_requested: Callable[[], bool] = lambda: False,
        next_stats_page_requested: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a resource-independent engine with injectable hotkeys and time."""
        settings.validate()
        self.settings = settings
        self.vision = vision
        self.dashboard = dashboard
        self.stats = stats
        self.recorder = recorder
        self.start_mode = start_mode
        self.stop_requested = stop_requested
        self.pause_requested = pause_requested
        self.debug_requested = debug_requested
        self.stats_requested = stats_requested
        self.previous_stats_page_requested = previous_stats_page_requested
        self.next_stats_page_requested = next_stats_page_requested
        self.clock = clock
        self.sleep = sleep
        self.controller = FishingController(settings.controller_profile)
        self.calibrator = BarCalibrator()
        self.state = AutomationState.STARTUP
        self._state_started_seconds = clock()
        self._encounter: EncounterContext | None = None
        self._encounter_sequence = 0
        self._last_duty_ratio = 0.0
        self._cast_charge_ratio = 0.0
        self._catch_progress_ratio = 0.0
        self._detector_confidence = 0.0
        self._bite_deadline_seconds: float | None = None
        self._observation_sequence = 0
        self._debug_warnings = 0
        self._last_recorded_progress_ratio = 0.0
        self._dashboard_view = DashboardView.CURRENT
        self._historical_stats: HistoricalStatsSnapshot | None = None
        self._history_page = 0
        self._verified_max_hold_seconds: deque[float] = deque(maxlen=5)
        self._previous_bar_length_pixels: int | None = None
        self._control_phase = ControlPhase.PERFECT
        self._perfect_status = PerfectStatus.ELIGIBLE
        self._containment_margin_pixels: float | None = None
        self._first_bite_positive_seconds: float | None = None
        self._treasure_status = TreasureStatus.NONE
        self._treasure_attempt_active = False
        self._treasure_attempt_started_seconds: float | None = None
        self._treasure_retry_after_seconds = 0.0
        self._treasure_contact_seconds = 0.0
        self._treasure_missing_frames = 0
        self._treasure_last_cycle_seconds: float | None = None
        self._treasure_last_seen_seconds: float | None = None
        self._treasure_last_center_y_pixels: float | None = None
        self._treasure_last_top_y_pixels: float | None = None
        self._treasure_last_bottom_y_pixels: float | None = None
        self._treasure_absence_started_seconds: float | None = None
        self._treasure_bad_fish_frames = 0
        self._energy_ratio: float | None = None
        self._energy_status = EnergyStatus.UNKNOWN

    def run(self, input_controller: InputPort) -> EngineResult:
        """Run the state machine and always idle input before returning."""
        result = EngineResult.STOPPED
        try:
            if not self._recover_open_loot_menu(input_controller):
                self._record_inventory_full_stop()
                return EngineResult.INVENTORY_FULL
            initial = self.vision.observe_scene(self._manual_bar_length())
            if initial.ui_detected:
                self._begin_encounter_if_needed()
                self._transition(AutomationState.CALIBRATING, "Fishing minigame detected")
            elif self.settings.automation_mode is AutomationMode.CONTINUOUS:
                self._transition(AutomationState.STARTUP, "Automatic cast begins in 3 seconds")
            elif self.settings.automation_mode is AutomationMode.HOOK_ONLY:
                self._start_bite_wait("Waiting for your cast and a bite")
            else:
                self._transition(
                    AutomationState.WAITING_FOR_MINIGAME, "Waiting for the fishing minigame"
                )

            while True:
                try:
                    self._check_hotkeys(input_controller)
                    if self.state is AutomationState.STARTUP:
                        safe_stop = self._ensure_energy(input_controller)
                        if safe_stop is not None:
                            result = safe_stop
                            break
                        self._transition(
                            AutomationState.STARTUP,
                            "Energy verified; automatic cast begins in 3 seconds",
                        )
                        self._interruptible_wait(3.0, input_controller)
                        self._perform_cast(input_controller)
                        self._start_bite_wait("Cast complete; waiting for a bite")
                    elif self.state is AutomationState.CASTING:
                        self._perform_cast(input_controller)
                        self._start_bite_wait("Cast complete; waiting for a bite")
                    elif self.state is AutomationState.WAITING_FOR_BITE:
                        if self._wait_for_bite(input_controller):
                            self._transition(
                                AutomationState.HOOKING, "Bite confirmed; setting the hook"
                            )
                        else:
                            result = EngineResult.BITE_TIMEOUT
                            break
                    elif self.state is AutomationState.HOOKING:
                        hook_started_seconds = self.clock()
                        self._tap_interruptibly(input_controller, 0.04)
                        if self._encounter is not None:
                            self._encounter.bite_monotonic_seconds = hook_started_seconds
                            self._update_encounter("bite_at_utc", self._now_utc())
                        if self.recorder is not None:
                            self.recorder.record(
                                "hook",
                                self.state.value,
                                hook_started_seconds,
                                {
                                    "encounter_id": self._current_encounter_id(),
                                    "first_positive_to_mouse_down_milliseconds": (
                                        (hook_started_seconds - self._first_bite_positive_seconds)
                                        * 1000.0
                                        if self._first_bite_positive_seconds is not None
                                        else None
                                    ),
                                },
                            )
                        self._transition(
                            AutomationState.WAITING_FOR_MINIGAME,
                            "Hook sent; waiting for the minigame",
                        )
                    elif self.state is AutomationState.WAITING_FOR_MINIGAME:
                        next_state = self._wait_for_minigame(input_controller)
                        self._transition(next_state, self._message_for(next_state))
                    elif self.state in {AutomationState.CALIBRATING, AutomationState.FISHING}:
                        outcome = self._run_minigame(input_controller)
                        if outcome is AutomationState.READING_RESULT:
                            self._transition(outcome, "Reading the catch result")
                        else:
                            safe_stop = self._after_encounter(input_controller)
                            if safe_stop is not None:
                                result = safe_stop
                                break
                    elif self.state is AutomationState.READING_RESULT:
                        if self._collect_result(input_controller):
                            self._dismiss_result(input_controller)
                            if not self._loot_treasure(
                                input_controller,
                                required=bool(
                                    self._encounter is not None
                                    and self._encounter.treasure_collected
                                ),
                            ):
                                self._record_inventory_full_stop()
                                result = EngineResult.INVENTORY_FULL
                                break
                        safe_stop = self._after_encounter(input_controller)
                        if safe_stop is not None:
                            result = safe_stop
                            break
                    elif self.state is AutomationState.REFUELING:
                        safe_stop = self._ensure_energy(input_controller)
                        if safe_stop is not None:
                            result = safe_stop
                            break
                        if self.settings.automation_mode is AutomationMode.CONTINUOUS:
                            self._transition(
                                AutomationState.STARTUP, "Preparing the next cast"
                            )
                        elif self.settings.automation_mode is AutomationMode.HOOK_ONLY:
                            self._start_bite_wait("Waiting for your next cast")
                        else:
                            self._transition(
                                AutomationState.WAITING_FOR_MINIGAME,
                                "Waiting for a minigame",
                            )
                except _StateRedirect as redirect:
                    self._transition(redirect.state, self._message_for(redirect.state))
        except _StopRequested:
            result = EngineResult.STOPPED
        finally:
            self._send_input(input_controller, "idle")
            self._transition(AutomationState.STOPPED, "Stopped safely")
        return result

    def _perform_cast(self, input_controller: InputPort) -> CastReleaseMethod:
        self._begin_encounter_if_needed()
        self._transition(AutomationState.CASTING, "Charging cast; watching for MAX")
        self.vision.begin_cast()
        self._check_hotkeys(input_controller)
        self._send_input(input_controller, "press")
        started_seconds = self.clock()
        confirmed_meter = False
        full_readings = 0
        reversal_readings = 0
        peak_charge = 0.0
        cast_frame_sequence = 0
        release_method: CastReleaseMethod | None = None
        try:
            while release_method is None:
                self._check_hotkeys(input_controller)
                elapsed_seconds = self.clock() - started_seconds
                observation = self.vision.observe_cast()
                cast_frame_sequence += 1
                self._record_cast_observation(observation, cast_frame_sequence)
                self._cast_charge_ratio = observation.charge_ratio
                self._detector_confidence = observation.confidence
                self._publish("Charging cast")
                if observation.meter_detected and observation.confidence >= 0.55:
                    confirmed_meter = True
                    peak_charge = max(peak_charge, observation.charge_ratio)
                    full_readings = full_readings + 1 if observation.charge_ratio >= 0.99 else 0
                    if peak_charge >= 0.99 and observation.charge_ratio < peak_charge:
                        reversal_readings = 1
                    if elapsed_seconds >= 0.50 and full_readings >= 2:
                        release_method = CastReleaseMethod.VISUAL_MAX
                    elif elapsed_seconds >= 0.50 and reversal_readings >= 1:
                        release_method = CastReleaseMethod.VISUAL_REVERSAL
                elif not confirmed_meter and elapsed_seconds >= self._fallback_hold_seconds():
                    release_method = CastReleaseMethod.TIMED_FALLBACK
                if elapsed_seconds >= 2.5:
                    release_method = CastReleaseMethod.SAFETY_TIMEOUT
                if release_method is None:
                    self._interruptible_wait(0.01, input_controller)
        finally:
            self._send_input(input_controller, "release")
        assert release_method is not None
        held_seconds = self.clock() - started_seconds
        max_verification = self._verify_max_indicator(input_controller, release_method)
        if max_verification is MaxVerification.VERIFIED:
            self._verified_max_hold_seconds.append(held_seconds)
        if self._encounter is not None:
            self._encounter.cast_started_monotonic_seconds = started_seconds
            self._encounter.cast_release_method = release_method
            self._update_encounter("cast_started_at_utc", self._now_utc())
            self._update_encounter("cast_release_method", release_method.value)
            self._encounter.max_verification = max_verification
            self._update_encounter("max_verification", max_verification.value)
        self.dashboard.log(
            f"Cast released by {release_method.value} ({max_verification.value} MAX)"
        )
        if self.recorder is not None:
            self.recorder.record(
                "cast-release",
                self.state.value,
                self.clock(),
                {
                    "encounter_id": self._current_encounter_id(),
                    "release_method": release_method.value,
                    "peak_charge_ratio": peak_charge,
                    "meter_confirmed": confirmed_meter,
                    "mouse_down_seconds": held_seconds,
                    "max_verification": max_verification.value,
                    "fallback_seconds": self._fallback_hold_seconds(),
                },
            )
        return release_method

    def _fallback_hold_seconds(self) -> float:
        """Return an explicit override or a verified-MAX adaptive duration."""
        if self.settings.cast_hold_seconds_explicit:
            return self.settings.cast_hold_seconds
        if not self._verified_max_hold_seconds:
            return 1.10
        return max(
            0.95,
            min(1.25, statistics.median(self._verified_max_hold_seconds)),
        )

    def _verify_max_indicator(
        self,
        input_controller: InputPort,
        release_method: CastReleaseMethod,
    ) -> MaxVerification:
        """Read the post-release MAX label without ever recasting on failure."""
        self._check_hotkeys(input_controller)
        if self.vision.detect_text("MAX"):
            return MaxVerification.VERIFIED
        if release_method in {
            CastReleaseMethod.VISUAL_MAX,
            CastReleaseMethod.VISUAL_REVERSAL,
        }:
            return MaxVerification.ESTIMATED
        if release_method is CastReleaseMethod.TIMED_FALLBACK and self._verified_max_hold_seconds:
            return MaxVerification.ESTIMATED
        return MaxVerification.UNKNOWN

    def _wait_for_bite(self, input_controller: InputPort) -> bool:
        deadline = self._bite_deadline_seconds or (self.clock() + self.BITE_TIMEOUT_SECONDS)
        confirmations = 0
        previous_positive: BiteObservation | None = None
        next_deadline = self.clock()
        while self.clock() < deadline:
            self._check_hotkeys(input_controller)
            observation = self.vision.observe_bite()
            if self.recorder is not None:
                bite_frame = self.vision.latest_frame()
                self.recorder.record(
                    "bite-observation",
                    self.state.value,
                    self.clock(),
                    {
                        "bite_detected": observation.bite_detected,
                        "confidence": observation.confidence,
                        "bounds": observation.bounds,
                        "icon_center_pixels": observation.icon_center_pixels,
                        "encounter_id": self._current_encounter_id(),
                    },
                    image=("bite", bite_frame)
                    if observation.bite_detected and bite_frame is not None
                    else None,
                )
            spatially_consistent = (
                observation.bite_detected
                and observation.icon_center_pixels is not None
                and previous_positive is not None
                and previous_positive.icon_center_pixels is not None
                and abs(
                    observation.icon_center_pixels[0]
                    - previous_positive.icon_center_pixels[0]
                )
                <= 3.0
                and abs(
                    observation.icon_center_pixels[1]
                    - previous_positive.icon_center_pixels[1]
                )
                <= 3.0
            )
            if observation.bite_detected:
                if not spatially_consistent:
                    self._first_bite_positive_seconds = self.clock()
                confirmations = confirmations + 1 if spatially_consistent else 1
                previous_positive = observation
                if confirmations >= 2:
                    return True
            else:
                confirmations = 0
                previous_positive = None
                self._first_bite_positive_seconds = None
            self._bite_deadline_seconds = deadline
            self._publish("Waiting for a bite")
            next_deadline = self._sleep_to_deadline(next_deadline, input_controller)
        self._send_input(input_controller, "idle")
        self.dashboard.log("No bite arrived within 45 seconds", level="warning")
        if self._encounter is not None:
            self._encounter.outcome = EncounterOutcome.TIMED_OUT
            self._update_encounter("outcome", EncounterOutcome.TIMED_OUT.value)
            self._update_encounter("ended_at_utc", self._now_utc())
            self._record_terminal_outcome(EncounterOutcome.TIMED_OUT)
        return False

    def _wait_for_minigame(self, input_controller: InputPort) -> AutomationState:
        wait_indefinitely = (
            self.settings.automation_mode is AutomationMode.MINIGAME_ONLY
            and self._encounter is None
        )
        deadline = self.clock() + 2.5
        result_scan_sequence = 0
        while wait_indefinitely or self.clock() < deadline:
            self._check_hotkeys(input_controller)
            observation = self.vision.observe_scene(self._manual_bar_length())
            self._record_observation(observation)
            if observation.ui_detected:
                self._begin_encounter_if_needed()
                if self._encounter is not None:
                    self._encounter.fight_started_monotonic_seconds = self.clock()
                    self._update_encounter("minigame_started_at_utc", self._now_utc())
                return AutomationState.CALIBRATING
            catch = self.vision.read_catch()
            result_scan_sequence += 1
            self._record_result_scan(catch, result_scan_sequence)
            if catch.card_detected:
                return AutomationState.READING_RESULT
            self._interruptible_wait(0.04, input_controller)
        # Trash and algae can skip the minigame. Give the result reader its
        # normal polling window and register an unknown instead of silently
        # dropping an encounter when recognition remains uncertain.
        return AutomationState.READING_RESULT

    def _run_minigame(self, input_controller: InputPort) -> AutomationState:
        self.controller.reset()
        self.calibrator.reset()
        self._last_recorded_progress_ratio = 0.0
        active_length = self._manual_bar_length() or self._previous_bar_length_pixels
        calibration_complete = self._manual_bar_length() is not None
        self._control_phase = ControlPhase.PERFECT
        self._perfect_status = PerfectStatus.ELIGIBLE
        self._containment_margin_pixels = None
        self._first_bite_positive_seconds = None
        self._reset_treasure_tracking()
        if self._encounter is not None:
            self._encounter.perfect_status = PerfectStatus.ELIGIBLE
            self._encounter.containment_breaks = 0
            self._encounter.minimum_margin_pixels = None
            self._encounter.unreliable_edge_frames = 0
            self._encounter.treasure_status = TreasureStatus.NONE
            self._encounter.treasure_attempts = 0
            self._encounter.treasure_collected = False
            self._encounter.treasure_looted = False
        missing_ui_frames = 0
        unreliable_frames = 0
        peak_progress = 0.0
        peak_progress_confidence = 0.0
        next_deadline = self.clock()
        while True:
            cycle_started = perf_counter()
            controller_milliseconds = 0.0
            input_milliseconds = 0.0
            self._check_hotkeys(input_controller)
            observation = self.vision.observe_scene(active_length)
            self._record_observation(observation)
            if not observation.ui_detected:
                missing_ui_frames += 1
                if missing_ui_frames == 1:
                    self._send_input(input_controller, "duty", self._last_duty_ratio)
                elif missing_ui_frames == 2:
                    self._send_input(input_controller, "duty", 0.50)
                else:
                    self._send_input(input_controller, "idle")
                if missing_ui_frames >= 3:
                    break
            else:
                missing_ui_frames = 0
                if (
                    observation.progress_confidence >= 0.55
                    and observation.progress_ratio >= peak_progress
                ):
                    peak_progress = observation.progress_ratio
                    peak_progress_confidence = observation.progress_confidence
                self._catch_progress_ratio = observation.progress_ratio
                if not calibration_complete:
                    calibrated_length = self.calibrator.observe(
                        observation.bar_length_pixels
                    )
                    if active_length is None and observation.bar_length_pixels is not None:
                        active_length = observation.bar_length_pixels
                        self._transition(
                            AutomationState.FISHING,
                            "Fishing with provisional bar calibration",
                        )
                    if calibrated_length is not None:
                        active_length = calibrated_length
                        self._previous_bar_length_pixels = calibrated_length
                        calibration_complete = True
                        self._transition(
                            AutomationState.FISHING, "Fishing control active"
                        )
                if observation.control_ready and active_length is not None:
                    unreliable_frames = 0
                    self._update_perfect_eligibility(observation, peak_progress)
                    now_seconds = self.clock()
                    treasure_target_pixels = self._choose_treasure_target(
                        observation,
                        active_length,
                        now_seconds,
                    )
                    controller_started = perf_counter()
                    duty_ratio = self.controller.step(
                        observation,
                        active_length,
                        now_seconds,
                        self._control_phase,
                        treasure_target_pixels,
                    )
                    controller_milliseconds = (perf_counter() - controller_started) * 1000.0
                    self._last_duty_ratio = duty_ratio
                    self._detector_confidence = min(
                        observation.fish_confidence, observation.bar_confidence
                    )
                    input_milliseconds = self._send_input(
                        input_controller, "duty", duty_ratio
                    )
                    self._record_control_decision()
                    if (
                        self.start_mode is StartMode.DEBUG
                        and (duty_ratio <= 0.001 or duty_ratio >= 0.999)
                    ):
                        self._debug_warnings += 1
                else:
                    self._observe_treasure_without_control(observation, self.clock())
                    unreliable_frames += 1
                    if unreliable_frames == 1:
                        input_milliseconds = self._send_input(
                            input_controller, "duty", self._last_duty_ratio
                        )
                    elif unreliable_frames == 2:
                        input_milliseconds = self._send_input(
                            input_controller, "duty", 0.50
                        )
                    else:
                        input_milliseconds = self._send_input(input_controller, "idle")
                        self.controller.reset()
                if self._treasure_attempt_active:
                    message = "Collecting treasure; fish reserve protected"
                elif self._control_phase is ControlPhase.PERFECT:
                    message = "Protecting Perfect catch"
                else:
                    message = "Perfect missed; securing the catch"
                self._publish(message)
            self._record_cycle_timing(
                observation,
                controller_milliseconds,
                input_milliseconds,
                (perf_counter() - cycle_started) * 1000.0,
            )
            next_deadline = self._sleep_to_deadline(next_deadline, input_controller)

        self._send_input(input_controller, "idle")
        if self._treasure_status in {TreasureStatus.SEEN, TreasureStatus.TARGETING}:
            self._set_treasure_status(TreasureStatus.ABANDONED)
        self._confirm_perfect_indicator(input_controller)
        if self._encounter is not None:
            duration_ms = self._fight_duration_milliseconds()
            self._update_encounter("fight_milliseconds", duration_ms)
            self._encounter.peak_progress_ratio = peak_progress
            self._encounter.peak_progress_confidence = peak_progress_confidence
            self._encounter.perfect_status = self._perfect_status
            self._update_encounter("perfect_status", self._perfect_status.value)
            self._update_encounter(
                "containment_breaks", self._encounter.containment_breaks
            )
            self._update_encounter(
                "minimum_margin_pixels", self._encounter.minimum_margin_pixels
            )
            if self.recorder is not None:
                self.recorder.record(
                    "perfect-status",
                    self.state.value,
                    self.clock(),
                    {
                        "encounter_id": self._current_encounter_id(),
                        "perfect_status": self._perfect_status.value,
                        "containment_breaks": self._encounter.containment_breaks,
                        "minimum_margin_pixels": self._encounter.minimum_margin_pixels,
                        "unreliable_edge_frames": self._encounter.unreliable_edge_frames,
                    },
                )
        # A disappearing minigame can mean either catch or escape. Resolve it
        # against the result card before falling back to progress evidence.
        return AutomationState.READING_RESULT

    def _choose_treasure_target(
        self,
        observation: FishingObservation,
        bar_length_pixels: int,
        timestamp_seconds: float,
    ) -> float | None:
        """Return a fish-safe chest target, or ``None`` to keep following the fish.

        ReelPilot first attempts a center that can contain both sprites. A separated
        chest is pursued only with a nearly full progress reserve, calm fish motion,
        and a short deadline. Progress or motion danger aborts the attempt immediately.
        """
        self._observe_treasure_without_control(observation, timestamp_seconds)
        current_chest_is_reliable = (
            observation.treasure_center_y_pixels is not None
            and observation.treasure_top_y_pixels is not None
            and observation.treasure_bottom_y_pixels is not None
            and observation.treasure_confidence >= 0.65
        )
        latch_is_fresh = (
            self._treasure_attempt_active
            and self._treasure_last_seen_seconds is not None
            and timestamp_seconds - self._treasure_last_seen_seconds <= 0.20
        )
        if current_chest_is_reliable:
            treasure_center_y_pixels = observation.treasure_center_y_pixels
            treasure_top_y_pixels = observation.treasure_top_y_pixels
            treasure_bottom_y_pixels = observation.treasure_bottom_y_pixels
        elif latch_is_fresh:
            treasure_center_y_pixels = self._treasure_last_center_y_pixels
            treasure_top_y_pixels = self._treasure_last_top_y_pixels
            treasure_bottom_y_pixels = self._treasure_last_bottom_y_pixels
        else:
            treasure_center_y_pixels = None
            treasure_top_y_pixels = None
            treasure_bottom_y_pixels = None

        fish_is_reliable = (
            observation.fish_top_y_pixels is not None
            and observation.fish_bottom_y_pixels is not None
            and observation.fish_confidence >= 0.70
        )
        if fish_is_reliable:
            self._treasure_bad_fish_frames = 0
        else:
            self._treasure_bad_fish_frames += 1

        if (
            self._treasure_status in {TreasureStatus.NONE, TreasureStatus.COLLECTED}
            or treasure_center_y_pixels is None
            or treasure_top_y_pixels is None
            or treasure_bottom_y_pixels is None
            or not fish_is_reliable
        ):
            if not latch_is_fresh or self._treasure_bad_fish_frames >= 3:
                self._stop_treasure_attempt(timestamp_seconds)
            return None

        assert observation.fish_top_y_pixels is not None
        assert observation.fish_bottom_y_pixels is not None

        half_bar_pixels = bar_length_pixels / 2.0
        guard_pixels = max(2.0, bar_length_pixels * 0.04)
        # Legal bar centers which contain both the fish and chest with a small
        # interior guard. This is the only treasure path allowed without spending
        # catch progress or Perfect eligibility.
        lower_center_pixels = max(
            observation.fish_bottom_y_pixels + guard_pixels - half_bar_pixels,
            treasure_bottom_y_pixels + 2.0 - half_bar_pixels,
            self.controller.TRACK_TOP_PIXELS + half_bar_pixels,
        )
        upper_center_pixels = min(
            observation.fish_top_y_pixels - guard_pixels + half_bar_pixels,
            treasure_top_y_pixels - 2.0 + half_bar_pixels,
            self.controller.TRACK_BOTTOM_PIXELS - half_bar_pixels,
        )
        current_margin_is_safe = (
            observation.containment_margin_pixels is not None
            and observation.containment_margin_pixels >= guard_pixels
        )
        attempts_available = (
            self._encounter is None or self._encounter.treasure_attempts < 2
        )
        if (
            lower_center_pixels <= upper_center_pixels
            and (self._treasure_attempt_active or current_margin_is_safe)
            and attempts_available
        ):
            self._start_treasure_attempt(timestamp_seconds)
            return (lower_center_pixels + upper_center_pixels) / 2.0

        previous = self.controller.last_decision
        fish_is_darting = bool(
            previous is not None
            and previous.active_profile.value == "darting"
        )
        attempt_elapsed = (
            timestamp_seconds - self._treasure_attempt_started_seconds
            if self._treasure_attempt_started_seconds is not None
            else 0.0
        )
        if self._treasure_attempt_active:
            unsafe = (
                observation.progress_ratio <= 0.72
                or observation.progress_confidence < 0.70
                or self._treasure_bad_fish_frames >= 3
                or fish_is_darting
                or attempt_elapsed >= 2.50
                or (
                    observation.containment_margin_pixels is not None
                    and observation.containment_margin_pixels <= 0.0
                )
            )
            if unsafe:
                self._stop_treasure_attempt(timestamp_seconds)
                return None
            return treasure_center_y_pixels

        can_spend_reserve = (
            timestamp_seconds >= self._treasure_retry_after_seconds
            and observation.progress_confidence >= 0.70
            and observation.progress_ratio >= 0.98
            and current_chest_is_reliable
            and not fish_is_darting
            and current_margin_is_safe
            and self._encounter is not None
            and self._encounter.treasure_attempts < 2
        )
        if can_spend_reserve:
            self._start_treasure_attempt(timestamp_seconds)
            return treasure_center_y_pixels
        return None

    def _observe_treasure_without_control(
        self,
        observation: FishingObservation,
        timestamp_seconds: float,
    ) -> None:
        """Update chest visibility and conservative collection evidence."""
        elapsed_seconds = (
            0.02
            if self._treasure_last_cycle_seconds is None
            else max(
                0.0,
                min(0.10, timestamp_seconds - self._treasure_last_cycle_seconds),
            )
        )
        self._treasure_last_cycle_seconds = timestamp_seconds
        visible = (
            observation.ui_detected
            and observation.treasure_center_y_pixels is not None
            and observation.treasure_top_y_pixels is not None
            and observation.treasure_bottom_y_pixels is not None
            and observation.treasure_confidence >= 0.65
        )
        if visible:
            assert observation.treasure_top_y_pixels is not None
            assert observation.treasure_bottom_y_pixels is not None
            self._treasure_missing_frames = 0
            self._treasure_absence_started_seconds = None
            self._treasure_last_seen_seconds = timestamp_seconds
            self._treasure_last_center_y_pixels = observation.treasure_center_y_pixels
            self._treasure_last_top_y_pixels = observation.treasure_top_y_pixels
            self._treasure_last_bottom_y_pixels = observation.treasure_bottom_y_pixels
            if self._treasure_status is TreasureStatus.NONE:
                self._set_treasure_status(TreasureStatus.SEEN)
                self.dashboard.log("Treasure chest detected; fish remains the priority")
            contained = (
                observation.bar_top_y_pixels is not None
                and observation.bar_bottom_y_pixels is not None
                and observation.bar_top_y_pixels
                <= observation.treasure_top_y_pixels + 2.0
                and observation.bar_bottom_y_pixels
                >= observation.treasure_bottom_y_pixels - 2.0
            )
            if self._treasure_attempt_active and contained:
                self._treasure_contact_seconds += elapsed_seconds
            return

        if self._treasure_status in {
            TreasureStatus.SEEN,
            TreasureStatus.TARGETING,
        }:
            self._treasure_missing_frames += 1
            if not observation.ui_detected or self._treasure_contact_seconds < 0.45:
                self._treasure_absence_started_seconds = None
                return
            if self._treasure_absence_started_seconds is None:
                self._treasure_absence_started_seconds = timestamp_seconds
            if timestamp_seconds - self._treasure_absence_started_seconds >= 0.15:
                self._treasure_attempt_active = False
                self._set_treasure_status(TreasureStatus.COLLECTED)
                self.dashboard.log("Treasure chest secured; finishing the fish")

    def _start_treasure_attempt(self, timestamp_seconds: float) -> None:
        """Latch one bounded chest attempt without double-counting frames."""
        if self._treasure_attempt_active:
            return
        if self._encounter is not None and self._encounter.treasure_attempts >= 2:
            return
        self._treasure_attempt_active = True
        self._treasure_attempt_started_seconds = timestamp_seconds
        self._treasure_contact_seconds = 0.0
        self._treasure_absence_started_seconds = None
        if self._encounter is not None:
            self._encounter.treasure_attempts += 1
            self._update_encounter(
                "treasure_attempts", self._encounter.treasure_attempts
            )
            if self.stats is not None:
                self.stats.record_treasure_attempt()
        self._set_treasure_status(TreasureStatus.TARGETING)

    def _stop_treasure_attempt(self, timestamp_seconds: float) -> None:
        """Return control to the fish and delay a possible retry."""
        if not self._treasure_attempt_active:
            return
        self._treasure_attempt_active = False
        self._treasure_attempt_started_seconds = None
        self._treasure_retry_after_seconds = timestamp_seconds + 0.60
        self._treasure_contact_seconds = 0.0
        self._treasure_absence_started_seconds = None
        if self._treasure_status is TreasureStatus.TARGETING:
            self._set_treasure_status(TreasureStatus.SEEN)

    def _set_treasure_status(self, status: TreasureStatus) -> None:
        """Persist one treasure-state transition and update live counters once."""
        if self._treasure_status is status:
            return
        previous = self._treasure_status
        self._treasure_status = status
        if self._encounter is not None:
            self._encounter.treasure_status = status
            self._update_encounter("treasure_status", status.value)
            if previous is TreasureStatus.NONE and status is TreasureStatus.SEEN:
                self._update_encounter("treasure_seen", 1)
                if self.stats is not None:
                    self.stats.record_treasure_seen()
            if status is TreasureStatus.COLLECTED:
                self._encounter.treasure_collected = True
                self._update_encounter("treasure_collected", 1)
                if self.stats is not None:
                    self.stats.record_treasure_collected()
        if self.recorder is not None:
            self.recorder.record(
                "treasure-status",
                self.state.value,
                self.clock(),
                {
                    "encounter_id": self._current_encounter_id(),
                    "treasure_status": status.value,
                    "treasure_attempts": (
                        self._encounter.treasure_attempts
                        if self._encounter is not None
                        else 0
                    ),
                    "contact_seconds": self._treasure_contact_seconds,
                },
            )

    def _reset_treasure_tracking(self) -> None:
        """Reset all per-minigame chest evidence."""
        self._treasure_status = TreasureStatus.NONE
        self._treasure_attempt_active = False
        self._treasure_attempt_started_seconds = None
        self._treasure_retry_after_seconds = 0.0
        self._treasure_contact_seconds = 0.0
        self._treasure_missing_frames = 0
        self._treasure_last_cycle_seconds = None
        self._treasure_last_seen_seconds = None
        self._treasure_last_center_y_pixels = None
        self._treasure_last_top_y_pixels = None
        self._treasure_last_bottom_y_pixels = None
        self._treasure_absence_started_seconds = None
        self._treasure_bad_fish_frames = 0

    def _update_perfect_eligibility(
        self,
        observation: FishingObservation,
        peak_progress_ratio: float | None = None,
    ) -> None:
        """Update containment evidence without treating edge jitter as a break."""
        encounter = self._encounter
        reliable_edges = (
            observation.fish_confidence >= 0.70
            and observation.bar_confidence >= 0.70
            and observation.containment_margin_pixels is not None
        )
        if not reliable_edges:
            if encounter is not None:
                encounter.unreliable_edge_frames += 1
            if self._perfect_status is PerfectStatus.ELIGIBLE:
                self._perfect_status = PerfectStatus.UNKNOWN
            return
        margin_pixels = observation.containment_margin_pixels
        assert margin_pixels is not None
        self._containment_margin_pixels = margin_pixels
        if encounter is not None and (
            encounter.minimum_margin_pixels is None
            or margin_pixels < encounter.minimum_margin_pixels
        ):
            encounter.minimum_margin_pixels = margin_pixels
        progress_confirms_break = (
            peak_progress_ratio is not None
            and observation.progress_confidence >= 0.70
            and peak_progress_ratio - observation.progress_ratio >= 0.004
        )
        # Fish and bar edges are integer-pixel masks whose boundary can jitter by
        # several pixels under animation. A deep gap is decisive immediately; a
        # shallow gap needs an independently observed progress regression. This
        # prevents an ordinary fish at a track stop from disabling Perfect control
        # on the first frame while still switching promptly after a real escape.
        containment_broke = margin_pixels < -6.0 or (
            margin_pixels < -2.0 and progress_confirms_break
        )
        if containment_broke and self._control_phase is ControlPhase.PERFECT:
            self._control_phase = ControlPhase.RECOVERY
            self._perfect_status = PerfectStatus.MISSED
            if encounter is not None:
                encounter.containment_breaks += 1
            self.dashboard.log(
                "Perfect eligibility lost; switching to catch recovery",
                level="warning",
            )

    def _confirm_perfect_indicator(self, input_controller: InputPort) -> None:
        """Promote eligibility only when bounded OCR sees Stardew's Perfect label."""
        if self._perfect_status is PerfectStatus.MISSED:
            return
        self._check_hotkeys(input_controller)
        if self.vision.detect_text("Perfect"):
            self._perfect_status = PerfectStatus.CONFIRMED

    def _collect_result(self, input_controller: InputPort) -> bool:
        deadline = self.clock() + 2.5
        result_scan_sequence = 0
        while self.clock() < deadline:
            self._check_hotkeys(input_controller)
            catch = self.vision.read_catch()
            result_scan_sequence += 1
            self._record_result_scan(catch, result_scan_sequence)
            if catch.card_detected:
                self._record_catch_observation(catch)
                self._register_catch(catch)
                return True
            self._interruptible_wait(0.05, input_controller)
        # Progress can briefly approach full immediately before a loss.  Without
        # an actual result card there is nothing safe to dismiss or recognize;
        # treating that case as a catch caused a blind click to start a weak cast.
        self._register_escape()
        return False

    def _loot_treasure(
        self,
        input_controller: InputPort,
        *,
        required: bool = True,
    ) -> bool:
        """Inspect and transfer an item-grab menu, preserving blocked loot.

        Stardew's item-grab menu moves a source stack into the backpack with one
        ordinary click.  ReelPilot clicks each source coordinate at most once, then
        waits for two empty observations before pressing Escape.  A post-Escape scan
        proves that the menu actually closed; if a full backpack caused the stack to
        return to its source slot, the menu stays open and automation stops.
        """
        deadline_seconds = self.clock() + (4.0 if required else 2.50)
        menu_seen = False
        treasure_context = bool(
            self._encounter is not None
            and (
                self._encounter.treasure_attempts > 0
                or self._encounter.treasure_status is not TreasureStatus.NONE
            )
        )
        empty_readings = 0
        attempted_centers: set[tuple[int, int]] = set()
        scan_sequence = 0
        while self.clock() < deadline_seconds:
            observation = self._observe_treasure_loot_safely(input_controller)
            scan_sequence += 1
            self._record_treasure_loot_observation(observation, scan_sequence)
            if not observation.menu_detected or observation.confidence < 0.65:
                if menu_seen and empty_readings >= 2:
                    if required or treasure_context:
                        self._mark_treasure_looted()
                    return True
                self._interruptible_wait(0.08, input_controller)
                continue
            if not menu_seen:
                self._transition(
                    AutomationState.LOOTING_TREASURE,
                    "Transferring item-grab loot",
                )
                if treasure_context and self._treasure_status is not TreasureStatus.COLLECTED:
                    self._set_treasure_status(TreasureStatus.COLLECTED)
            menu_seen = True
            occupied = observation.occupied_slot_centers_pixels
            if not occupied:
                empty_readings += 1
                if empty_readings >= 2:
                    # Escape first resolves a cursor-held stack. This menu may
                    # remain open even when it is genuinely empty, so only click
                    # a structurally located OK button after the post-Escape scan
                    # proves that no source item returned.
                    self._tap_key_interruptibly(input_controller, 0x1B)
                    self._interruptible_wait(0.20, input_controller)
                    verification = self._observe_treasure_loot_safely(
                        input_controller
                    )
                    scan_sequence += 1
                    self._record_treasure_loot_observation(
                        verification,
                        scan_sequence,
                    )
                    if (
                        not verification.menu_detected
                        or verification.confidence < 0.65
                    ):
                        if required or treasure_context:
                            self._mark_treasure_looted()
                        return True
                    # Escape returns a cursor-held stack to the source when the
                    # backpack cannot accept it.  Do not click the same stack again.
                    if verification.occupied_slot_centers_pixels:
                        break
                    close_center = verification.close_button_center_pixels
                    if close_center is None:
                        break
                    self._click_interruptibly(
                        input_controller,
                        close_center[0],
                        close_center[1],
                    )
                    self._interruptible_wait(0.20, input_controller)
                    closed = self._observe_treasure_loot_safely(input_controller)
                    scan_sequence += 1
                    self._record_treasure_loot_observation(closed, scan_sequence)
                    if not closed.menu_detected or closed.confidence < 0.65:
                        if required or treasure_context:
                            self._mark_treasure_looted()
                        return True
                    if closed.occupied_slot_centers_pixels:
                        break
                    empty_readings = 0
                self._interruptible_wait(0.08, input_controller)
                continue
            empty_readings = 0
            next_center = next(
                (center for center in occupied if center not in attempted_centers),
                None,
            )
            if next_center is None:
                break
            attempted_centers.add(next_center)
            self._check_hotkeys(input_controller)
            self._click_interruptibly(
                input_controller,
                next_center[0],
                next_center[1],
            )
            self._interruptible_wait(0.08, input_controller)

        if not menu_seen and not required:
            return True
        self.dashboard.log(
            "Inventory cannot accept the remaining loot; the item-grab menu was left open.",
            level="warning",
        )
        return False

    def _recover_open_loot_menu(self, input_controller: InputPort) -> bool:
        """Transfer a menu left open by a prior stop before inspecting or casting.

        This is a single cheap probe on ordinary startup. If an item-grab menu is
        visible, the normal guarded transfer loop owns it; a blocked transfer leaves
        the menu open and prevents every later state from issuing a cast or click.
        """
        observation = self._observe_treasure_loot_safely(input_controller)
        if not observation.menu_detected or observation.confidence < 0.65:
            return True
        self.dashboard.log("Recovering an open item-grab menu before fishing")
        return self._loot_treasure(input_controller, required=False)

    def _observe_treasure_loot_safely(
        self,
        input_controller: InputPort,
    ) -> TreasureLootObservation:
        """Park the animated cursor, allow one frame, then inspect the loot menu."""
        self._check_hotkeys(input_controller)
        input_controller.prepare_menu_capture()
        self._check_hotkeys(input_controller)
        self._interruptible_wait(0.03, input_controller)
        observation = self.vision.observe_treasure_loot()
        self._check_hotkeys(input_controller)
        return observation

    def _record_inventory_full_stop(self) -> None:
        """Record one inventory-full stop without dismissing the blocked menu."""
        if self.stats is not None:
            self.stats.record_inventory_full_stop()
        if self.recorder is not None:
            self.recorder.record(
                "inventory-stop",
                self.state.value,
                self.clock(),
                {
                    "reason": "inventory-full",
                    "encounter_id": self._current_encounter_id(),
                },
            )

    def _mark_treasure_looted(self) -> None:
        """Persist successful menu transfer exactly once."""
        encounter = self._encounter
        if encounter is None or encounter.treasure_looted:
            return
        encounter.treasure_looted = True
        encounter.treasure_status = TreasureStatus.LOOTED
        self._treasure_status = TreasureStatus.LOOTED
        self._update_encounter("treasure_status", TreasureStatus.LOOTED.value)
        self._update_encounter("treasure_looted", 1)
        if self.stats is not None:
            self.stats.record_treasure_looted()
        if self.recorder is not None:
            self.recorder.record(
                "treasure-status",
                self.state.value,
                self.clock(),
                {
                    "encounter_id": encounter.encounter_id,
                    "treasure_status": TreasureStatus.LOOTED.value,
                    "treasure_attempts": encounter.treasure_attempts,
                    "looted": True,
                },
            )
        self.dashboard.log("Treasure loot transferred safely")

    def _record_treasure_loot_observation(
        self,
        observation: object,
        scan_sequence: int,
    ) -> None:
        """Record bounded loot-menu evidence without coupling recorder to vision."""
        if self.recorder is None:
            return
        if not isinstance(observation, TreasureLootObservation):
            return
        frame = self.vision.latest_frame()
        image = (
            ("treasure-loot", frame)
            if frame is not None and (scan_sequence % 3 == 0 or observation.menu_detected)
            else None
        )
        self.recorder.record(
            "treasure-loot",
            self.state.value,
            self.clock(),
            {
                "encounter_id": self._current_encounter_id(),
                "menu_detected": observation.menu_detected,
                "source_slot_centers_pixels": observation.source_slot_centers_pixels,
                "occupied_slot_centers_pixels": observation.occupied_slot_centers_pixels,
                "inventory_slot_count": observation.inventory_slot_count,
                "occupied_inventory_slot_count": (
                    observation.occupied_inventory_slot_count
                ),
                "inventory_full": observation.inventory_full,
                "close_button_center_pixels": (
                    observation.close_button_center_pixels
                ),
                "confidence": observation.confidence,
            },
            image=image,
        )

    def _register_escape(self) -> None:
        encounter = self._encounter
        if encounter is None or encounter.result_registered:
            return
        if encounter.fight_started_monotonic_seconds is None:
            encounter.perfect_status = PerfectStatus.UNKNOWN
            self._update_encounter("perfect_status", PerfectStatus.UNKNOWN.value)
        encounter.result_registered = True
        encounter.outcome = EncounterOutcome.ESCAPED
        self._update_encounter("outcome", EncounterOutcome.ESCAPED.value)
        self._update_encounter("ended_at_utc", self._now_utc())
        if self.stats is not None:
            self.stats.record_escape(encounter.encounter_id)
        self._record_terminal_outcome(EncounterOutcome.ESCAPED)
        self.dashboard.log("Fish escaped", level="warning")

    def _register_catch(self, catch: CatchObservation) -> None:
        self._begin_encounter_if_needed()
        encounter = self._encounter
        if encounter is None or encounter.result_registered:
            return
        encounter.result_registered = True
        encounter.outcome = (
            EncounterOutcome.FISH
            if catch.result_type is ResultType.FISH
            else EncounterOutcome.ITEM
            if catch.result_type is ResultType.ITEM
            else EncounterOutcome.UNKNOWN
        )
        self._update_encounter("outcome", encounter.outcome.value)
        self._update_encounter("ended_at_utc", self._now_utc())
        self._record_terminal_outcome(encounter.outcome)
        fight_ms = self._fight_duration_milliseconds()
        cast_to_result_ms = None
        if encounter.cast_started_monotonic_seconds is not None:
            cast_to_result_ms = round(
                (self.clock() - encounter.cast_started_monotonic_seconds) * 1000
            )
        if self.stats is not None:
            record = CatchRecord(
                self.stats.new_event_id(),
                encounter.encounter_id,
                self._now_utc(),
                catch.result_type,
                catch.name,
                catch.length_inches,
                max(1, catch.quantity),
                catch.confidence,
                catch.status,
                fight_ms,
                cast_to_result_ms,
                encounter.cast_release_method,
                self.settings.automation_mode,
                self.settings.controller_profile,
                encounter.perfect_status,
                encounter.max_verification,
                encounter.containment_breaks,
                encounter.minimum_margin_pixels,
            )
            latest_card = self.vision.latest_catch_card()
            self.stats.record_catch(record, latest_card)
        if catch.result_type is ResultType.UNKNOWN:
            self.dashboard.log("Uncertain catch outcome recorded", level="warning")
        else:
            self.dashboard.log(
                f"Caught {catch.name or 'an unknown result'}"
                + (f" — {catch.length_inches} in." if catch.length_inches else "")
            )

    def _record_cast_observation(
        self, observation: CastObservation, frame_sequence: int
    ) -> None:
        if self.recorder is None:
            return
        frame = self.vision.latest_frame()
        image = None
        if frame is not None and (frame_sequence % 4 == 0 or observation.meter_detected):
            if observation.bounds is None:
                image = ("cast-full", frame)
            else:
                left, top, right, bottom = observation.bounds
                if frame.shape[:2] == (bottom - top, right - left):
                    image = ("cast-meter", frame[:, :, :3])
                else:
                    image = ("cast-meter", frame[top:bottom, left:right, :3])
        self.recorder.record(
            "cast-observation",
            self.state.value,
            self.clock(),
            {
                "meter_detected": observation.meter_detected,
                "charge_ratio": observation.charge_ratio,
                "confidence": observation.confidence,
                "bounds": observation.bounds,
                "fill_width_pixels": observation.fill_width_pixels,
                "track_width_pixels": observation.track_width_pixels,
                "tracking_confidence": observation.tracking_confidence,
                "encounter_id": self._current_encounter_id(),
            },
            image=image,
        )

    def _record_catch_observation(self, catch: CatchObservation) -> None:
        if self.recorder is None:
            return
        card = self.vision.latest_catch_card()
        self.recorder.record(
            "catch-result",
            self.state.value,
            self.clock(),
            {
                "result_type": catch.result_type.value,
                "card_detected": catch.card_detected,
                "name": catch.name,
                "length_inches": catch.length_inches,
                "quantity": catch.quantity,
                "confidence": catch.confidence,
                "recognition_status": catch.status.value,
                "bounds": catch.bounds,
                "encounter_id": self._current_encounter_id(),
            },
            image=("catch-card", card) if card is not None else None,
        )

    def _record_result_scan(self, catch: CatchObservation, scan_sequence: int) -> None:
        if self.recorder is None:
            return
        frame = self.vision.latest_frame()
        image = None
        if frame is not None and (catch.card_detected or scan_sequence % 4 == 0):
            image = ("result-scan", frame)
        self.recorder.record(
            "result-scan",
            self.state.value,
            self.clock(),
            {
                "card_detected": catch.card_detected,
                "recognition_status": catch.status.value,
                "bounds": catch.bounds,
                "encounter_id": self._current_encounter_id(),
            },
            image=image,
        )

    def _dismiss_result(self, input_controller: InputPort) -> None:
        self._interruptible_wait(0.75, input_controller)
        self._tap_interruptibly(input_controller, 0.04)
        self._interruptible_wait(0.75, input_controller)

    def _ensure_energy(self, input_controller: InputPort) -> EngineResult | None:
        """Check energy at a safe boundary and refuel only after consensus.

        Two reliable readings below 33 percent are required before ReelPilot touches
        the food slot. Three consecutive unreadable frames stop the session instead
        of risking a cast with unknown energy.
        """
        if not self.settings.auto_eat:
            return None
        reliable_ratios: list[float] = []
        low_readings = 0
        high_readings = 0
        consecutive_unreadable = 0
        for sequence in range(1, 4):
            self._check_hotkeys(input_controller)
            observation = self.vision.observe_energy()
            self._record_energy_observation(observation, sequence)
            reliable = observation.meter_detected and observation.confidence >= 0.70
            if reliable:
                ratio = min(1.0, max(0.0, observation.fill_ratio))
                reliable_ratios.append(ratio)
                consecutive_unreadable = 0
                if ratio < 0.33:
                    low_readings += 1
                else:
                    high_readings += 1
                self._energy_ratio = ratio
                if self.stats is not None:
                    self.stats.record_energy(ratio)
            else:
                consecutive_unreadable += 1
            self._check_hotkeys(input_controller)
            if sequence < 3:
                self._interruptible_wait(0.04, input_controller)

        if consecutive_unreadable >= 3 or (low_readings < 2 and high_readings < 2):
            self._energy_status = EnergyStatus.UNKNOWN
            self.dashboard.log(
                "Energy meter could not be read reliably; no cast was started.",
                level="warning",
            )
            return EngineResult.ENERGY_UNREADABLE

        self._energy_ratio = statistics.median(reliable_ratios)
        if low_readings < 2:
            self._energy_status = EnergyStatus.OK
            self._publish("Energy verified; ready to fish")
            return None

        self._energy_status = EnergyStatus.LOW
        return self._refuel(input_controller)

    def _refuel(self, input_controller: InputPort) -> EngineResult | None:
        """Eat confirmed food until energy reaches 75 percent or a safety limit."""
        started_seconds = self.clock()
        consumed = 0
        self._energy_status = EnergyStatus.REFUELING
        self._transition(
            AutomationState.REFUELING,
            "Energy below 33%; refueling from the reserved food slot",
        )
        self._send_input(input_controller, "idle")

        while consumed < 4 and self.clock() - started_seconds < 15.0:
            before_ratio = self._energy_ratio
            if before_ratio is None:
                self._select_hotbar_slot(input_controller, self.settings.rod_slot)
                return EngineResult.ENERGY_UNREADABLE
            if before_ratio >= 0.75:
                self._select_hotbar_slot(input_controller, self.settings.rod_slot)
                self._energy_status = EnergyStatus.OK
                self._publish("Energy restored; rod reselected")
                return None

            self._select_hotbar_slot(input_controller, self.settings.food_slot)
            # The action applies to the selected food independent of cursor position.
            # Keep the cursor in the supported playfield, away from hotbar/HUD controls.
            self._right_click_interruptibly(input_controller, 640, 420)

            prompt_deadline = min(started_seconds + 15.0, self.clock() + 1.50)
            prompt = None
            while self.clock() < prompt_deadline:
                self._check_hotkeys(input_controller)
                candidate = self.vision.observe_food_prompt()
                self._record_food_prompt_observation(candidate)
                self._check_hotkeys(input_controller)
                if (
                    candidate.prompt_detected
                    and candidate.confidence >= 0.70
                    and candidate.yes_center_pixels is not None
                ):
                    prompt = candidate
                    break
                self._interruptible_wait(0.05, input_controller)

            if prompt is None or prompt.yes_center_pixels is None:
                self._select_hotbar_slot(input_controller, self.settings.rod_slot)
                self._energy_status = EnergyStatus.LOW
                self.dashboard.log(
                    "Reserved food slot did not produce a confirmed eating prompt.",
                    level="warning",
                )
                return EngineResult.FOOD_UNAVAILABLE

            yes_x_pixels, yes_y_pixels = prompt.yes_center_pixels
            self._click_interruptibly(input_controller, yes_x_pixels, yes_y_pixels)
            verification_deadline = min(started_seconds + 15.0, self.clock() + 3.50)
            verified_ratio: float | None = None
            while self.clock() < verification_deadline:
                self._check_hotkeys(input_controller)
                observation = self.vision.observe_energy()
                self._record_energy_observation(observation, consumed + 4)
                self._check_hotkeys(input_controller)
                if observation.meter_detected and observation.confidence >= 0.70:
                    ratio = min(1.0, max(0.0, observation.fill_ratio))
                    self._energy_ratio = ratio
                    if self.stats is not None:
                        self.stats.record_energy(ratio)
                    if ratio < before_ratio - 0.01:
                        self._select_hotbar_slot(
                            input_controller, self.settings.rod_slot
                        )
                        self._energy_status = EnergyStatus.LOW
                        return EngineResult.FOOD_UNAVAILABLE
                    if ratio >= before_ratio + 0.03:
                        verified_ratio = ratio
                        break
                self._interruptible_wait(0.05, input_controller)

            if verified_ratio is None:
                self._select_hotbar_slot(input_controller, self.settings.rod_slot)
                self._energy_status = EnergyStatus.LOW
                self.dashboard.log(
                    "Eating did not produce a verified energy increase.",
                    level="warning",
                )
                return EngineResult.FOOD_UNAVAILABLE

            consumed += 1
            if self.stats is not None:
                self.stats.record_food_consumed()
            if self.recorder is not None:
                self.recorder.record(
                    "food-consumed",
                    self.state.value,
                    self.clock(),
                    {
                        "food_slot": self.settings.food_slot,
                        "before_ratio": before_ratio,
                        "after_ratio": verified_ratio,
                        "session_food_consumed": consumed,
                    },
                )
            self.dashboard.log(
                f"Food consumed; energy is {verified_ratio:.0%}"
            )
            self._publish("Refueling safely")

        self._select_hotbar_slot(input_controller, self.settings.rod_slot)
        if self._energy_ratio is not None and self._energy_ratio >= 0.75:
            self._energy_status = EnergyStatus.OK
            return None
        self._energy_status = EnergyStatus.LOW
        self.dashboard.log(
            "Energy did not reach 75% within the food/time limit.",
            level="warning",
        )
        return EngineResult.FOOD_UNAVAILABLE

    def _record_energy_observation(self, observation: object, sequence: int) -> None:
        """Persist low-rate energy evidence without entering the control hot path."""
        if self.recorder is None or not isinstance(observation, EnergyObservation):
            return
        frame = self.vision.latest_frame()
        image = None
        if frame is not None and (sequence == 1 or not observation.meter_detected):
            image = ("energy-hud", frame)
        self.recorder.record(
            "energy-observation",
            self.state.value,
            self.clock(),
            {
                "meter_detected": observation.meter_detected,
                "fill_ratio": observation.fill_ratio,
                "confidence": observation.confidence,
                "bounds": observation.bounds,
            },
            image=image,
        )

    def _record_food_prompt_observation(self, observation: object) -> None:
        """Persist prompt geometry and OCR confidence for safe offline tuning."""
        if self.recorder is None or not isinstance(observation, FoodPromptObservation):
            return
        frame = self.vision.latest_frame()
        self.recorder.record(
            "food-prompt",
            self.state.value,
            self.clock(),
            {
                "prompt_detected": observation.prompt_detected,
                "confidence": observation.confidence,
                "bounds": observation.bounds,
                "yes_center_pixels": observation.yes_center_pixels,
            },
            image=("food-prompt", frame) if frame is not None else None,
        )

    def _after_encounter(
        self, input_controller: InputPort
    ) -> EngineResult | None:
        self._send_input(input_controller, "idle")
        self._encounter = None
        self.calibrator.reset()
        self.controller.reset()
        self._catch_progress_ratio = 0.0
        self._control_phase = ControlPhase.PERFECT
        self._perfect_status = PerfectStatus.ELIGIBLE
        self._containment_margin_pixels = None
        self._reset_treasure_tracking()
        if self.settings.automation_mode is AutomationMode.CONTINUOUS:
            self._transition(AutomationState.STARTUP, "Preparing the next cast")
        elif self.settings.automation_mode is AutomationMode.HOOK_ONLY:
            safe_stop = self._ensure_energy(input_controller)
            if safe_stop is not None:
                return safe_stop
            self._start_bite_wait("Waiting for your next cast")
        else:
            safe_stop = self._ensure_energy(input_controller)
            if safe_stop is not None:
                return safe_stop
            self._transition(AutomationState.WAITING_FOR_MINIGAME, "Waiting for a minigame")
        return None

    def _pause(self, input_controller: InputPort) -> AutomationState:
        previous = self.state
        self._send_input(input_controller, "idle")
        if previous is AutomationState.REFUELING:
            if self.stop_requested():
                raise _StopRequested
            input_controller.tap_key(self._hotbar_virtual_key(self.settings.rod_slot))
            if self.stop_requested():
                raise _StopRequested
        self._transition(AutomationState.PAUSED, "Paused; input released")
        self.dashboard.log("Automation paused; all input released")
        next_result_scan_seconds = self.clock()
        result_scan_sequence = 0
        while True:
            if self.stop_requested():
                raise _StopRequested
            if self.stats_requested():
                self._toggle_stats_history()
                self._publish("Paused; input released")
            if self._dashboard_view is DashboardView.HISTORY:
                self._change_history_page()
            # Stardew's minigame does not pause with ReelPilot. If F7 is pressed
            # during a fight, the result card can appear while input remains safely
            # released. Poll at 5 Hz so the catch is recorded before the player opens
            # the Journal or otherwise dismisses it; the encounter latch prevents a
            # second row when normal result handling resumes.
            now_seconds = self.clock()
            if (
                self._encounter is not None
                and not self._encounter.result_registered
                and now_seconds >= next_result_scan_seconds
            ):
                catch = self.vision.read_catch()
                result_scan_sequence += 1
                self._record_result_scan(catch, result_scan_sequence)
                if catch.card_detected:
                    self._record_catch_observation(catch)
                    self._register_catch(catch)
                next_result_scan_seconds = now_seconds + 0.20
            if self.pause_requested():
                break
            self.sleep(0.05)
        self._dashboard_view = DashboardView.CURRENT
        observation = self.vision.observe_scene(self._manual_bar_length())
        if observation.ui_detected:
            self.controller.reset()
            self.calibrator.reset()
            return AutomationState.CALIBRATING
        catch = self.vision.read_catch()
        if catch.card_detected:
            return AutomationState.READING_RESULT
        if previous is AutomationState.REFUELING:
            return AutomationState.REFUELING
        if previous in {
            AutomationState.CASTING,
            AutomationState.WAITING_FOR_BITE,
            AutomationState.HOOKING,
            AutomationState.WAITING_FOR_MINIGAME,
        }:
            self._bite_deadline_seconds = self.clock() + self.BITE_TIMEOUT_SECONDS
            return AutomationState.WAITING_FOR_BITE
        if self.settings.automation_mode is AutomationMode.CONTINUOUS:
            return AutomationState.STARTUP
        if self.settings.automation_mode is AutomationMode.HOOK_ONLY:
            return AutomationState.WAITING_FOR_BITE
        return AutomationState.WAITING_FOR_MINIGAME

    def _check_hotkeys(self, input_controller: InputPort) -> None:
        if self.stop_requested():
            raise _StopRequested
        if self.stats_requested():
            self.dashboard.log("Pause with F7 before viewing statistics")
        if self.debug_requested():
            self.dashboard.log("F6 Debug Start is available only from the ready screen")
        if self.pause_requested():
            raise _StateRedirect(self._pause(input_controller))

    def _interruptible_wait(self, duration_seconds: float, input_controller: InputPort) -> None:
        deadline = self.clock() + duration_seconds
        while self.clock() < deadline:
            self._check_hotkeys(input_controller)
            self.sleep(min(0.01, max(0.0, deadline - self.clock())))

    def _tap_interruptibly(self, input_controller: InputPort, duration_seconds: float) -> None:
        self._send_input(input_controller, "press")
        try:
            self._interruptible_wait(duration_seconds, input_controller)
        finally:
            self._send_input(input_controller, "release")

    def _tap_key_interruptibly(
        self,
        input_controller: InputPort,
        virtual_key: int,
    ) -> None:
        """Tap one menu key with stop checks on both sides."""
        self._check_hotkeys(input_controller)
        input_controller.tap_key(virtual_key)
        self._check_hotkeys(input_controller)
        if self.recorder is not None:
            self.recorder.record(
                "input-command",
                self.state.value,
                self.clock(),
                {
                    "encounter_id": self._current_encounter_id(),
                    "action": "key-tap",
                    "virtual_key": virtual_key,
                },
            )

    @staticmethod
    def _hotbar_virtual_key(slot: int) -> int:
        """Map Stardew's default hotbar slots 1-12 to Windows virtual keys."""
        if 1 <= slot <= 9:
            return ord(str(slot))
        if slot == 10:
            return ord("0")
        if slot == 11:
            return 0xBD  # VK_OEM_MINUS
        if slot == 12:
            return 0xBB  # VK_OEM_PLUS
        raise ValueError("hotbar slot must be between 1 and 12")

    def _select_hotbar_slot(
        self,
        input_controller: InputPort,
        slot: int,
    ) -> None:
        """Select one reserved hotbar slot with stop checks on both sides."""
        self._tap_key_interruptibly(input_controller, self._hotbar_virtual_key(slot))

    def _click_interruptibly(
        self,
        input_controller: InputPort,
        x_pixels: int,
        y_pixels: int,
    ) -> None:
        """Perform one bounded left click with emergency-stop checks."""
        self._check_hotkeys(input_controller)
        input_controller.click_at(x_pixels, y_pixels)
        self._check_hotkeys(input_controller)
        if self.recorder is not None:
            self.recorder.record(
                "input-command",
                self.state.value,
                self.clock(),
                {
                    "encounter_id": self._current_encounter_id(),
                    "action": "coordinate-click",
                    "x_pixels": x_pixels,
                    "y_pixels": y_pixels,
                },
            )

    def _right_click_interruptibly(
        self,
        input_controller: InputPort,
        x_pixels: int,
        y_pixels: int,
    ) -> None:
        """Perform one bounded right click with emergency-stop checks."""
        self._check_hotkeys(input_controller)
        input_controller.right_click_at(x_pixels, y_pixels)
        self._check_hotkeys(input_controller)
        if self.recorder is not None:
            self.recorder.record(
                "input-command",
                self.state.value,
                self.clock(),
                {
                    "encounter_id": self._current_encounter_id(),
                    "action": "right-click",
                    "x_pixels": x_pixels,
                    "y_pixels": y_pixels,
                },
            )

    def _sleep_to_deadline(
        self, previous_deadline: float, input_controller: InputPort
    ) -> float:
        deadline = max(previous_deadline + self.CONTROL_INTERVAL_SECONDS, self.clock())
        remaining = deadline - self.clock()
        if remaining > 0:
            self._interruptible_wait(remaining, input_controller)
        return deadline

    def _start_bite_wait(self, message: str) -> None:
        self._bite_deadline_seconds = self.clock() + self.BITE_TIMEOUT_SECONDS
        self._transition(AutomationState.WAITING_FOR_BITE, message)

    def _begin_encounter_if_needed(self) -> None:
        if self._encounter is not None:
            return
        self._encounter_sequence += 1
        encounter_id = uuid.uuid4().hex
        self._encounter = EncounterContext(
            encounter_id,
            self._encounter_sequence,
            self.clock(),
        )
        if self.stats is not None:
            self.stats.begin_encounter(encounter_id, self._encounter_sequence)

    def _manual_bar_length(self) -> int | None:
        level = self.settings.fishing_level
        return 72 + 6 * level if level is not None else None

    def _transition(self, state: AutomationState, message: str) -> None:
        self.state = state
        self._state_started_seconds = self.clock()
        self.dashboard.log(message)
        self._publish(message)
        if self.recorder is not None:
            self.recorder.record(
                "state",
                state.value,
                self.clock(),
                {"message": message, "encounter_id": self._current_encounter_id()},
            )

    def _publish(self, message: str) -> None:
        stats = self.stats.snapshot if self.stats is not None else StatsSnapshot()
        bite_remaining = None
        if self.state is AutomationState.WAITING_FOR_BITE and self._bite_deadline_seconds:
            bite_remaining = max(0.0, self._bite_deadline_seconds - self.clock())
        self.dashboard.publish(
            RuntimeSnapshot(
                state=self.state,
                state_elapsed_seconds=max(
                    0.0, self.clock() - self._state_started_seconds
                ),
                connected=True,
                paused=self.state is AutomationState.PAUSED,
                automation_mode=self.settings.automation_mode,
                controller_profile=self.settings.controller_profile,
                cast_charge_ratio=self._cast_charge_ratio,
                bite_seconds_remaining=bite_remaining,
                catch_progress_ratio=self._catch_progress_ratio,
                duty_ratio=self._last_duty_ratio,
                detector_confidence=self._detector_confidence,
                session_fish=stats.session_fish,
                session_items=stats.session_items,
                session_escapes=stats.session_escapes,
                lifetime_fish=stats.lifetime_fish,
                recent_catches=stats.recent_catches,
                message=message,
                start_mode=self.start_mode,
                recording_path=(
                    str(self.recorder.path) if self.recorder is not None else None
                ),
                recorded_image_bytes=(
                    self.recorder.recorded_image_bytes if self.recorder is not None else 0
                ),
                recording_image_limit_bytes=(
                    self.recorder.image_limit_bytes if self.recorder is not None else 0
                ),
                dropped_images=(
                    self.recorder.dropped_images if self.recorder is not None else 0
                ),
                debug_warnings=self._debug_warnings,
                dashboard_view=self._dashboard_view,
                historical_stats=self._historical_stats,
                history_page=self._history_page,
                control_phase=self._control_phase,
                perfect_status=self._perfect_status,
                containment_margin_pixels=self._containment_margin_pixels,
                session_perfect=stats.session_perfect,
                session_perfect_attempts=stats.session_perfect_attempts,
                lifetime_perfect=stats.lifetime_perfect,
                lifetime_perfect_attempts=stats.lifetime_perfect_attempts,
                treasure_status=self._treasure_status,
                session_treasure_seen=stats.session_treasure_seen,
                session_treasure_collected=stats.session_treasure_collected,
                session_treasure_looted=stats.session_treasure_looted,
                lifetime_treasure_seen=stats.lifetime_treasure_seen,
                lifetime_treasure_collected=stats.lifetime_treasure_collected,
                lifetime_treasure_looted=stats.lifetime_treasure_looted,
                energy_ratio=self._energy_ratio,
                energy_status=self._energy_status,
                session_food_consumed=stats.session_food_consumed,
                lifetime_food_consumed=stats.lifetime_food_consumed,
                session_inventory_full_stops=stats.session_inventory_full_stops,
                lifetime_inventory_full_stops=(
                    stats.lifetime_inventory_full_stops
                ),
            )
        )

    def _toggle_stats_history(self) -> None:
        """Refresh or close history while automation is already safely paused."""
        if self._dashboard_view is DashboardView.HISTORY:
            self._dashboard_view = DashboardView.CURRENT
            return
        if self.stats is None:
            self.dashboard.log("Statistics are disabled", level="warning")
            return
        try:
            self._historical_stats = self.stats.refresh_history(timeout_seconds=2.0)
        except (OSError, sqlite3.Error, TimeoutError) as exc:
            self.dashboard.log(f"Statistics refresh failed: {exc}", level="warning")
            return
        self._history_page = 0
        self._dashboard_view = DashboardView.HISTORY
        self.dashboard.log("Historical statistics refreshed")

    def _change_history_page(self) -> None:
        """Consume Page Up/Down and clamp the species page to valid bounds."""
        if self._historical_stats is None:
            return
        maximum_page = max(0, (len(self._historical_stats.species) - 1) // 8)
        changed = False
        if self.previous_stats_page_requested():
            self._history_page = max(0, self._history_page - 1)
            changed = True
        if self.next_stats_page_requested():
            self._history_page = min(maximum_page, self._history_page + 1)
            changed = True
        if changed:
            self._publish("Paused; input released")

    def _record_observation(self, observation: FishingObservation) -> None:
        if self.recorder is None:
            return
        self._observation_sequence += 1
        frame = self.vision.latest_frame()
        image = None
        cadence = 5 if self.start_mode is StartMode.DEBUG else 10
        progress_regression = (
            observation.progress_confidence >= 0.55
            and self._last_recorded_progress_ratio - observation.progress_ratio >= 0.08
        )
        if observation.progress_confidence >= 0.55:
            self._last_recorded_progress_ratio = observation.progress_ratio
        anomaly = (
            not observation.ui_detected
            or not observation.control_ready
            or progress_regression
        )
        if self.start_mode is StartMode.DEBUG and anomaly:
            self._debug_warnings += 1
        if frame is not None and (
            (observation.ui_detected and self._observation_sequence % cadence == 0)
            or (self.start_mode is StartMode.DEBUG and anomaly)
        ):
            image = ("fishing-ui", frame)
        self.recorder.record(
            "observation",
            self.state.value,
            self.clock(),
            {
                "observation": {
                    "ui_detected": observation.ui_detected,
                    "fish_center_y_pixels": observation.fish_center_y_pixels,
                    "bar_center_y_pixels": observation.bar_center_y_pixels,
                    "bar_length_pixels": observation.bar_length_pixels,
                    "progress_ratio": observation.progress_ratio,
                    "progress_confidence": observation.progress_confidence,
                    "fish_confidence": observation.fish_confidence,
                    "bar_confidence": observation.bar_confidence,
                    "used_roi_capture": observation.used_roi_capture,
                    "fish_top_y_pixels": observation.fish_top_y_pixels,
                    "fish_bottom_y_pixels": observation.fish_bottom_y_pixels,
                    "bar_top_y_pixels": observation.bar_top_y_pixels,
                    "bar_bottom_y_pixels": observation.bar_bottom_y_pixels,
                    "containment_margin_pixels": observation.containment_margin_pixels,
                    "treasure_center_y_pixels": observation.treasure_center_y_pixels,
                    "treasure_top_y_pixels": observation.treasure_top_y_pixels,
                    "treasure_bottom_y_pixels": observation.treasure_bottom_y_pixels,
                    "treasure_confidence": observation.treasure_confidence,
                },
                "encounter_id": self._current_encounter_id(),
                "anomalies": {
                    "unreliable_detection": not observation.control_ready,
                    "progress_regression": progress_regression,
                },
                "timings_ms": {
                    "capture": observation.capture_milliseconds,
                    "detection": observation.detection_milliseconds,
                },
            },
            image=image,
        )

    def _record_cycle_timing(
        self,
        observation: FishingObservation,
        controller_milliseconds: float,
        input_milliseconds: float,
        total_milliseconds: float,
    ) -> None:
        if self.recorder is None:
            return
        self.recorder.record(
            "cycle-timing",
            self.state.value,
            self.clock(),
            {
                "encounter_id": self._current_encounter_id(),
                "timings_ms": {
                    "capture": observation.capture_milliseconds,
                    "detection": observation.detection_milliseconds,
                    "controller": controller_milliseconds,
                    "input": input_milliseconds,
                    "total": total_milliseconds,
                },
            },
        )

    def _send_input(
        self,
        input_controller: InputPort,
        action: str,
        duty_ratio: float | None = None,
    ) -> float:
        started = perf_counter()
        if action == "duty":
            if duty_ratio is None:
                raise ValueError("duty input requires a duty ratio")
            input_controller.set_duty(duty_ratio)
        elif action == "press":
            input_controller.press()
        elif action == "release":
            input_controller.release()
        elif action == "idle":
            input_controller.idle()
        else:
            raise ValueError(f"unsupported input action: {action}")
        elapsed_milliseconds = (perf_counter() - started) * 1000.0
        if self.recorder is not None:
            self.recorder.record(
                "input-command",
                self.state.value,
                self.clock(),
                {
                    "encounter_id": self._current_encounter_id(),
                    "action": action,
                    "duty_ratio": duty_ratio,
                    "input_milliseconds": elapsed_milliseconds,
                },
            )
        return elapsed_milliseconds

    def _record_control_decision(self) -> None:
        if self.recorder is None or self.controller.last_decision is None:
            return
        decision = self.controller.last_decision
        self.recorder.record(
            "control-decision",
            self.state.value,
            self.clock(),
            {
                "duty_ratio": decision.duty_ratio,
                "active_profile": decision.active_profile.value,
                "profile_blend_ratio": decision.profile_blend_ratio,
                "fish_position_pixels": decision.fish_position_pixels,
                "bar_position_pixels": decision.bar_position_pixels,
                "fish_velocity_pixels_per_second": decision.fish_velocity_pixels_per_second,
                "bar_velocity_pixels_per_second": decision.bar_velocity_pixels_per_second,
                "predicted_fish_pixels": decision.predicted_fish_pixels,
                "predicted_bar_pixels": decision.predicted_bar_pixels,
                "feasible_target_pixels": decision.feasible_target_pixels,
                "raw_center_error_pixels": decision.raw_center_error_pixels,
                "feasible_error_pixels": decision.feasible_error_pixels,
                "hover_trim_ratio": decision.hover_trim_ratio,
                "effective_hover_duty_ratio": decision.effective_hover_duty_ratio,
                "edge_clearance_ratio": decision.edge_clearance_ratio,
                "center_correction_ratio": decision.center_correction_ratio,
                "containment_margin_pixels": decision.containment_margin_pixels,
                "predicted_margin_pixels": decision.predicted_margin_pixels,
                "time_to_edge_seconds": decision.time_to_edge_seconds,
                "safety_override": decision.safety_override,
                "control_phase": decision.control_phase.value,
                "control_target": decision.control_target.value,
                "bar_length_pixels": self.calibrator.length_pixels
                or self._manual_bar_length(),
                "encounter_id": self._current_encounter_id(),
            },
        )

    def _current_encounter_id(self) -> str | None:
        return self._encounter.encounter_id if self._encounter is not None else None

    def _record_terminal_outcome(self, outcome: EncounterOutcome) -> None:
        if self.recorder is None:
            return
        self.recorder.record(
            "encounter-terminal",
            self.state.value,
            self.clock(),
            {
                "encounter_id": self._current_encounter_id(),
                "outcome": outcome.value,
            },
        )

    def _update_encounter(self, field: str, value: object) -> None:
        if self.stats is not None and self._encounter is not None:
            self.stats.repository.update_encounter(self._encounter.encounter_id, field, value)

    def _fight_duration_milliseconds(self) -> int | None:
        if self._encounter is None or self._encounter.fight_started_monotonic_seconds is None:
            return None
        return round((self.clock() - self._encounter.fight_started_monotonic_seconds) * 1000)

    @staticmethod
    def _now_utc() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _message_for(state: AutomationState) -> str:
        messages = {
            AutomationState.STARTUP: "Preparing an automatic cast",
            AutomationState.WAITING_FOR_BITE: "Waiting for a bite",
            AutomationState.WAITING_FOR_MINIGAME: "Waiting for the fishing minigame",
            AutomationState.CALIBRATING: "Calibrating the green bar",
            AutomationState.FISHING: "Fishing control active",
            AutomationState.READING_RESULT: "Reading the catch result",
            AutomationState.LOOTING_TREASURE: "Transferring treasure loot",
            AutomationState.REFUELING: "Refueling from the reserved food slot",
        }
        return messages.get(state, state.value)
