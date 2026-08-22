from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from ..control import FishingController
from ..domain import (
    AutomationMode,
    AutomationState,
    CastObservation,
    CastReleaseMethod,
    CatchObservation,
    EncounterContext,
    EncounterOutcome,
    FishingObservation,
    ReelPilotSettings,
    ResultType,
    RuntimeSnapshot,
)
from ..protocols import DashboardPort, InputPort, VisionPort
from ..recording import SessionRecorder
from ..stats import CatchRecord, StatsService, StatsSnapshot
from .calibration import BarCalibrator


class EngineResult(StrEnum):
    STOPPED = "stopped"
    BITE_TIMEOUT = "bite-timeout"


class _StopRequested(Exception):
    pass


class _StateRedirect(Exception):
    def __init__(self, state: AutomationState) -> None:
        self.state = state


class AutomationEngine:
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
        stop_requested: Callable[[], bool],
        pause_requested: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.vision = vision
        self.dashboard = dashboard
        self.stats = stats
        self.recorder = recorder
        self.stop_requested = stop_requested
        self.pause_requested = pause_requested
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

    def run(self, input_controller: InputPort) -> EngineResult:
        result = EngineResult.STOPPED
        try:
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
                        self._tap_interruptibly(input_controller, 0.04)
                        if self._encounter is not None:
                            self._encounter.bite_monotonic_seconds = self.clock()
                            self._update_encounter("bite_at_utc", self._now_utc())
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
                            self._after_encounter(input_controller)
                    elif self.state is AutomationState.READING_RESULT:
                        self._collect_result(input_controller)
                        self._dismiss_result(input_controller)
                        self._after_encounter(input_controller)
                except _StateRedirect as redirect:
                    self._transition(redirect.state, self._message_for(redirect.state))
        except _StopRequested:
            result = EngineResult.STOPPED
        finally:
            input_controller.idle()
            self._transition(AutomationState.STOPPED, "Stopped safely")
        return result

    def _perform_cast(self, input_controller: InputPort) -> CastReleaseMethod:
        self._begin_encounter_if_needed()
        self._transition(AutomationState.CASTING, "Charging cast; watching for MAX")
        self.vision.begin_cast()
        self._check_hotkeys(input_controller)
        input_controller.press()
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
                    full_readings = full_readings + 1 if observation.charge_ratio >= 0.98 else 0
                    if peak_charge >= 0.96 and observation.charge_ratio <= peak_charge - 0.02:
                        reversal_readings += 1
                    else:
                        reversal_readings = 0
                    if elapsed_seconds >= 0.50 and full_readings >= 2:
                        release_method = CastReleaseMethod.VISUAL_MAX
                    elif elapsed_seconds >= 0.50 and reversal_readings >= 2:
                        release_method = CastReleaseMethod.VISUAL_REVERSAL
                elif not confirmed_meter and elapsed_seconds >= self.settings.cast_hold_seconds:
                    release_method = CastReleaseMethod.TIMED_FALLBACK
                if elapsed_seconds >= 2.5:
                    release_method = CastReleaseMethod.SAFETY_TIMEOUT
                if release_method is None:
                    self._interruptible_wait(0.01, input_controller)
        finally:
            input_controller.release()
        assert release_method is not None
        if self._encounter is not None:
            self._encounter.cast_started_monotonic_seconds = started_seconds
            self._encounter.cast_release_method = release_method
            self._update_encounter("cast_started_at_utc", self._now_utc())
            self._update_encounter("cast_release_method", release_method.value)
        self.dashboard.log(f"Cast released by {release_method.value}")
        return release_method

    def _wait_for_bite(self, input_controller: InputPort) -> bool:
        deadline = self._bite_deadline_seconds or (self.clock() + self.BITE_TIMEOUT_SECONDS)
        confirmations = 0
        latched = False
        while self.clock() < deadline:
            self._check_hotkeys(input_controller)
            detected = self.vision.detect_bite()
            if self.recorder is not None:
                bite_frame = self.vision.latest_frame()
                self.recorder.record(
                    "bite-observation",
                    self.state.value,
                    self.clock(),
                    {"bite_detected": detected},
                    image=("bite", bite_frame) if detected and bite_frame is not None else None,
                )
            if detected and not latched:
                confirmations += 1
                if confirmations >= 2:
                    latched = True
                    return True
            elif not detected:
                confirmations = 0
            self._bite_deadline_seconds = deadline
            self._publish("Waiting for a bite")
            self._interruptible_wait(0.04, input_controller)
        input_controller.idle()
        self.dashboard.log("No bite arrived within 45 seconds", level="warning")
        if self._encounter is not None:
            self._encounter.outcome = EncounterOutcome.TIMED_OUT
            self._update_encounter("outcome", EncounterOutcome.TIMED_OUT.value)
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
                self._record_catch_observation(catch)
                return AutomationState.READING_RESULT
            self._interruptible_wait(0.04, input_controller)
        # Trash and algae can skip the minigame. Give the result reader its
        # normal polling window and register an unknown instead of silently
        # dropping an encounter when recognition remains uncertain.
        return AutomationState.READING_RESULT

    def _run_minigame(self, input_controller: InputPort) -> AutomationState:
        self.controller.reset()
        self.calibrator.reset()
        active_length = self._manual_bar_length()
        missing_ui_frames = 0
        unreliable_frames = 0
        peak_progress = 0.0
        next_deadline = self.clock()
        while True:
            self._check_hotkeys(input_controller)
            observation = self.vision.observe_scene(active_length)
            self._record_observation(observation)
            if not observation.ui_detected:
                missing_ui_frames += 1
                input_controller.idle()
                if missing_ui_frames >= 3:
                    break
            else:
                missing_ui_frames = 0
                peak_progress = max(peak_progress, observation.progress_ratio)
                self._catch_progress_ratio = observation.progress_ratio
                if active_length is None:
                    active_length = self.calibrator.observe(observation.bar_length_pixels)
                    if active_length is None:
                        input_controller.idle()
                        self._transition(
                            AutomationState.CALIBRATING, "Calibrating green bar length"
                        )
                        next_deadline = self._sleep_to_deadline(next_deadline, input_controller)
                        continue
                    self.controller.reset()
                    self._transition(AutomationState.FISHING, "Fishing control active")
                if observation.control_ready:
                    unreliable_frames = 0
                    duty_ratio = self.controller.step(observation, active_length, self.clock())
                    self._last_duty_ratio = duty_ratio
                    self._detector_confidence = min(
                        observation.fish_confidence, observation.bar_confidence
                    )
                    input_controller.set_duty(duty_ratio)
                    self._record_control_decision()
                else:
                    unreliable_frames += 1
                    if unreliable_frames == 1:
                        input_controller.set_duty(self._last_duty_ratio)
                    elif unreliable_frames == 2:
                        input_controller.set_duty(0.50)
                    else:
                        input_controller.idle()
                        self.controller.reset()
                self._publish("Fishing control active")
            next_deadline = self._sleep_to_deadline(next_deadline, input_controller)

        input_controller.idle()
        if self._encounter is not None:
            duration_ms = self._fight_duration_milliseconds()
            self._update_encounter("fight_milliseconds", duration_ms)
        if peak_progress >= 0.95:
            return AutomationState.READING_RESULT
        if self._encounter is not None:
            self._encounter.outcome = EncounterOutcome.ESCAPED
            if self.stats is not None:
                self.stats.record_escape(self._encounter.encounter_id)
        self.dashboard.log("Fish escaped", level="warning")
        return AutomationState.STARTUP

    def _collect_result(self, input_controller: InputPort) -> None:
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
                return
            self._interruptible_wait(0.05, input_controller)
        unknown = CatchObservation(True)
        self._record_catch_observation(unknown)
        self._register_catch(unknown)

    def _register_catch(self, catch: CatchObservation) -> None:
        self._begin_encounter_if_needed()
        encounter = self._encounter
        if encounter is None:
            return
        encounter.outcome = (
            EncounterOutcome.FISH
            if catch.result_type is ResultType.FISH
            else EncounterOutcome.ITEM
            if catch.result_type is ResultType.ITEM
            else EncounterOutcome.UNKNOWN
        )
        self._update_encounter("outcome", encounter.outcome.value)
        self._update_encounter("ended_at_utc", self._now_utc())
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
            )
            latest_card = self.vision.latest_catch_card()
            self.stats.record_catch(record, latest_card)
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
                "name": catch.name,
                "length_inches": catch.length_inches,
                "quantity": catch.quantity,
                "confidence": catch.confidence,
                "recognition_status": catch.status.value,
                "bounds": catch.bounds,
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
            },
            image=image,
        )

    def _dismiss_result(self, input_controller: InputPort) -> None:
        self._interruptible_wait(0.75, input_controller)
        self._tap_interruptibly(input_controller, 0.04)
        self._interruptible_wait(0.75, input_controller)

    def _after_encounter(self, input_controller: InputPort) -> None:
        input_controller.idle()
        self._encounter = None
        self.calibrator.reset()
        self.controller.reset()
        self._catch_progress_ratio = 0.0
        if self.settings.automation_mode is AutomationMode.CONTINUOUS:
            self._transition(AutomationState.STARTUP, "Preparing the next cast")
        elif self.settings.automation_mode is AutomationMode.HOOK_ONLY:
            self._start_bite_wait("Waiting for your next cast")
        else:
            self._transition(AutomationState.WAITING_FOR_MINIGAME, "Waiting for a minigame")

    def _pause(self, input_controller: InputPort) -> AutomationState:
        previous = self.state
        input_controller.idle()
        self._transition(AutomationState.PAUSED, "Paused; input released")
        self.dashboard.log("Automation paused; all input released")
        while True:
            if self.stop_requested():
                raise _StopRequested
            if self.pause_requested():
                break
            self.sleep(0.05)
        observation = self.vision.observe_scene(self._manual_bar_length())
        if observation.ui_detected:
            self.controller.reset()
            self.calibrator.reset()
            return AutomationState.CALIBRATING
        catch = self.vision.read_catch()
        if catch.card_detected:
            return AutomationState.READING_RESULT
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
        if self.pause_requested():
            raise _StateRedirect(self._pause(input_controller))

    def _interruptible_wait(self, duration_seconds: float, input_controller: InputPort) -> None:
        deadline = self.clock() + duration_seconds
        while self.clock() < deadline:
            self._check_hotkeys(input_controller)
            self.sleep(min(0.01, max(0.0, deadline - self.clock())))

    def _tap_interruptibly(self, input_controller: InputPort, duration_seconds: float) -> None:
        input_controller.press()
        try:
            self._interruptible_wait(duration_seconds, input_controller)
        finally:
            input_controller.release()

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
            self.recorder.record("state", state.value, self.clock(), {"message": message})

    def _publish(self, message: str) -> None:
        stats = self.stats.snapshot if self.stats is not None else StatsSnapshot()
        bite_remaining = None
        if self.state is AutomationState.WAITING_FOR_BITE and self._bite_deadline_seconds:
            bite_remaining = max(0.0, self._bite_deadline_seconds - self.clock())
        self.dashboard.publish(
            RuntimeSnapshot(
                self.state,
                max(0.0, self.clock() - self._state_started_seconds),
                True,
                self.state is AutomationState.PAUSED,
                self.settings.automation_mode,
                self.settings.controller_profile,
                self._cast_charge_ratio,
                bite_remaining,
                self._catch_progress_ratio,
                self._last_duty_ratio,
                self._detector_confidence,
                stats.session_fish,
                stats.session_items,
                stats.session_escapes,
                stats.lifetime_fish,
                stats.recent_catches,
                message,
            )
        )

    def _record_observation(self, observation: FishingObservation) -> None:
        if self.recorder is None:
            return
        self._observation_sequence += 1
        frame = self.vision.latest_frame()
        image = None
        if (
            observation.ui_detected
            and frame is not None
            and self._observation_sequence % 10 == 0
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
                    "fish_confidence": observation.fish_confidence,
                    "bar_confidence": observation.bar_confidence,
                },
                "timings_ms": {
                    "capture": observation.capture_milliseconds,
                    "detection": observation.detection_milliseconds,
                },
            },
            image=image,
        )

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
        }
        return messages.get(state, state.value)
