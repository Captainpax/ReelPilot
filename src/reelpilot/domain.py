from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class AutomationMode(StrEnum):
    CONTINUOUS = "continuous"
    HOOK_ONLY = "hook-only"
    MINIGAME_ONLY = "minigame-only"


class AutomationState(StrEnum):
    STARTUP = "startup"
    CASTING = "casting"
    WAITING_FOR_BITE = "waiting-for-bite"
    HOOKING = "hooking"
    WAITING_FOR_MINIGAME = "waiting-for-minigame"
    CALIBRATING = "calibrating"
    FISHING = "fishing"
    READING_RESULT = "reading-result"
    PAUSED = "paused"
    STOPPED = "stopped"


class ControllerProfile(StrEnum):
    AUTO = "auto"
    NORMAL = "normal"
    DARTING = "darting"


class EncounterOutcome(StrEnum):
    ACTIVE = "active"
    FISH = "fish"
    ITEM = "item"
    ESCAPED = "escaped"
    UNKNOWN = "unknown"
    TIMED_OUT = "timed-out"


class ResultType(StrEnum):
    FISH = "fish"
    ITEM = "item"
    UNKNOWN = "unknown"


class RecognitionStatus(StrEnum):
    RECOGNIZED = "recognized"
    AMBIGUOUS = "ambiguous"
    NO_TEXT = "no-text"
    UNAVAILABLE = "unavailable"


class CastReleaseMethod(StrEnum):
    VISUAL_MAX = "visual-max"
    VISUAL_REVERSAL = "visual-reversal"
    TIMED_FALLBACK = "timed-fallback"
    SAFETY_TIMEOUT = "safety-timeout"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class WindowBounds:
    left_pixels: int
    top_pixels: int
    right_pixels: int
    bottom_pixels: int

    @property
    def width_pixels(self) -> int:
        return self.right_pixels - self.left_pixels

    @property
    def height_pixels(self) -> int:
        return self.bottom_pixels - self.top_pixels


@dataclass(frozen=True, slots=True)
class FishingObservation:
    ui_detected: bool
    fish_center_y_pixels: float | None = None
    bar_center_y_pixels: float | None = None
    bar_length_pixels: int | None = None
    progress_ratio: float = 0.0
    fish_confidence: float = 0.0
    bar_confidence: float = 0.0
    capture_milliseconds: float = 0.0
    detection_milliseconds: float = 0.0

    @property
    def control_ready(self) -> bool:
        return (
            self.ui_detected
            and self.fish_center_y_pixels is not None
            and self.bar_center_y_pixels is not None
            and self.fish_confidence >= 0.55
            and self.bar_confidence >= 0.55
        )


@dataclass(frozen=True, slots=True)
class CastObservation:
    meter_detected: bool
    charge_ratio: float = 0.0
    confidence: float = 0.0
    bounds: tuple[int, int, int, int] | None = None
    fill_width_pixels: int | None = None
    track_width_pixels: int | None = None
    tracking_confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class CatchObservation:
    card_detected: bool
    result_type: ResultType = ResultType.UNKNOWN
    name: str | None = None
    length_inches: int | None = None
    quantity: int = 1
    confidence: float = 0.0
    status: RecognitionStatus = RecognitionStatus.NO_TEXT
    bounds: tuple[int, int, int, int] | None = None


@dataclass(slots=True)
class EncounterContext:
    encounter_id: str
    sequence: int
    started_monotonic_seconds: float
    cast_started_monotonic_seconds: float | None = None
    bite_monotonic_seconds: float | None = None
    fight_started_monotonic_seconds: float | None = None
    cast_release_method: CastReleaseMethod | None = None
    outcome: EncounterOutcome = EncounterOutcome.ACTIVE


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    state: AutomationState
    state_elapsed_seconds: float
    connected: bool
    paused: bool
    automation_mode: AutomationMode
    controller_profile: ControllerProfile
    cast_charge_ratio: float = 0.0
    bite_seconds_remaining: float | None = None
    catch_progress_ratio: float = 0.0
    duty_ratio: float = 0.0
    detector_confidence: float = 0.0
    session_fish: int = 0
    session_items: int = 0
    session_escapes: int = 0
    lifetime_fish: int = 0
    recent_catches: tuple[str, ...] = ()
    message: str = "Starting ReelPilot"


@dataclass(frozen=True, slots=True)
class ReelPilotSettings:
    automation_mode: AutomationMode = AutomationMode.CONTINUOUS
    controller_profile: ControllerProfile = ControllerProfile.AUTO
    fishing_level: int | None = None
    cast_hold_seconds: float = 1.5
    plain: bool = False
    stats_enabled: bool = True
    stats_directory: Path | None = None
    record_directory: Path | None = None

    def validate(self) -> None:
        if self.fishing_level is not None and not 0 <= self.fishing_level <= 10:
            raise ValueError("fishing_level must be between 0 and 10")
        if not 0.1 <= self.cast_hold_seconds <= 2.0:
            raise ValueError("cast_hold_seconds must be between 0.1 and 2.0")
        if (
            self.automation_mode is not AutomationMode.CONTINUOUS
            and self.cast_hold_seconds != 1.5
        ):
            raise ValueError("cast_hold_seconds is only valid in continuous mode")
