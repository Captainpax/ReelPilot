"""Core immutable domain types shared by ReelPilot subsystems.

The domain layer deliberately has no Windows, OpenCV, or SQLite dependencies. Keeping
the automation vocabulary here lets live components and offline tests exchange typed
observations without importing platform-specific code into the control loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .stats.models import HistoricalStatsSnapshot


class AutomationMode(StrEnum):
    """Select how much of the fishing workflow ReelPilot automates."""

    CONTINUOUS = "continuous"
    HOOK_ONLY = "hook-only"
    MINIGAME_ONLY = "minigame-only"


class AutomationState(StrEnum):
    """Represent the currently active automation state-machine node."""

    WAITING_FOR_GAME = "waiting-for-game"
    READY = "ready"
    STARTUP = "startup"
    CASTING = "casting"
    WAITING_FOR_BITE = "waiting-for-bite"
    HOOKING = "hooking"
    WAITING_FOR_MINIGAME = "waiting-for-minigame"
    CALIBRATING = "calibrating"
    FISHING = "fishing"
    READING_RESULT = "reading-result"
    LOOTING_TREASURE = "looting-treasure"
    REFUELING = "refueling"
    PAUSED = "paused"
    STOPPED = "stopped"


class StartMode(StrEnum):
    """Distinguish normal starts from diagnostic recording starts."""

    NORMAL = "normal"
    DEBUG = "debug"


class ControllerProfile(StrEnum):
    """Choose automatic, normal, or aggressive minigame control tuning."""

    AUTO = "auto"
    NORMAL = "normal"
    DARTING = "darting"


class EncounterOutcome(StrEnum):
    """Describe the terminal result of one cast or minigame encounter."""

    ACTIVE = "active"
    FISH = "fish"
    ITEM = "item"
    ESCAPED = "escaped"
    UNKNOWN = "unknown"
    TIMED_OUT = "timed-out"
    ABORTED = "aborted"


class DashboardView(StrEnum):
    """Choose the live-operation or historical-statistics dashboard."""

    CURRENT = "current"
    HISTORY = "history"


class DifficultyTier(StrEnum):
    """Group numeric fish difficulty into documented context-tag tiers."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    EXTREMELY_HARD = "extremely-hard"

    @classmethod
    def from_score(cls, score: int | None) -> DifficultyTier | None:
        """Return the vanilla difficulty tier for ``score``.

        Examples:
            >>> DifficultyTier.from_score(33)
            <DifficultyTier.EASY: 'easy'>
            >>> DifficultyTier.from_score(67)
            <DifficultyTier.HARD: 'hard'>

        """
        if score is None:
            return None
        if score <= 33:
            return cls.EASY
        if score <= 66:
            return cls.MEDIUM
        if score <= 100:
            return cls.HARD
        return cls.EXTREMELY_HARD


class ResultType(StrEnum):
    """Classify a recognized catch card."""

    FISH = "fish"
    ITEM = "item"
    UNKNOWN = "unknown"


class RecognitionStatus(StrEnum):
    """Explain why catch-card OCR was accepted or rejected."""

    RECOGNIZED = "recognized"
    AMBIGUOUS = "ambiguous"
    NO_TEXT = "no-text"
    UNAVAILABLE = "unavailable"


class CastReleaseMethod(StrEnum):
    """Record the evidence used to release a charged cast."""

    VISUAL_MAX = "visual-max"
    VISUAL_REVERSAL = "visual-reversal"
    TIMED_FALLBACK = "timed-fallback"
    SAFETY_TIMEOUT = "safety-timeout"
    PAUSED = "paused"


class MaxVerification(StrEnum):
    """Describe how confidently the game confirmed a maximum-power cast."""

    VERIFIED = "verified"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class ControlPhase(StrEnum):
    """Select containment protection or the proven catch-recovery controller."""

    PERFECT = "perfect"
    RECOVERY = "recovery"


class ControlTarget(StrEnum):
    """Identify whether a control decision is following the fish or a chest."""

    FISH = "fish"
    TREASURE = "treasure"


class PerfectStatus(StrEnum):
    """Track whether an encounter is still eligible for Stardew's Perfect bonus."""

    ELIGIBLE = "eligible"
    CONFIRMED = "confirmed"
    MISSED = "missed"
    UNKNOWN = "unknown"


class TreasureStatus(StrEnum):
    """Track the conservative lifecycle of one minigame treasure chest."""

    NONE = "none"
    SEEN = "seen"
    TARGETING = "targeting"
    COLLECTED = "collected"
    ABANDONED = "abandoned"
    LOOTED = "looted"


class EnergyStatus(StrEnum):
    """Describe whether the safe-boundary energy check permits another cast."""

    UNKNOWN = "unknown"
    OK = "ok"
    LOW = "low"
    REFUELING = "refueling"


@dataclass(frozen=True, slots=True)
class WindowBounds:
    """Describe an absolute Windows rectangle in screen pixels."""

    left_pixels: int
    top_pixels: int
    right_pixels: int
    bottom_pixels: int

    @property
    def width_pixels(self) -> int:
        """Return rectangle width in pixels."""
        return self.right_pixels - self.left_pixels

    @property
    def height_pixels(self) -> int:
        """Return rectangle height in pixels."""
        return self.bottom_pixels - self.top_pixels


@dataclass(frozen=True, slots=True)
class FishingObservation:
    """Contain one analyzed fishing-minigame frame."""

    ui_detected: bool
    fish_center_y_pixels: float | None = None
    bar_center_y_pixels: float | None = None
    bar_length_pixels: int | None = None
    progress_ratio: float = 0.0
    fish_confidence: float = 0.0
    bar_confidence: float = 0.0
    progress_confidence: float = 0.0
    capture_milliseconds: float = 0.0
    detection_milliseconds: float = 0.0
    used_roi_capture: bool = False
    fish_top_y_pixels: float | None = None
    fish_bottom_y_pixels: float | None = None
    bar_top_y_pixels: float | None = None
    bar_bottom_y_pixels: float | None = None
    containment_margin_pixels: float | None = None
    treasure_center_y_pixels: float | None = None
    treasure_top_y_pixels: float | None = None
    treasure_bottom_y_pixels: float | None = None
    treasure_confidence: float = 0.0

    @property
    def control_ready(self) -> bool:
        """Return whether positions are reliable enough for controller input."""
        return (
            self.ui_detected
            and self.fish_center_y_pixels is not None
            and self.bar_center_y_pixels is not None
            and self.fish_confidence >= 0.55
            and self.bar_confidence >= 0.55
        )


@dataclass(frozen=True, slots=True)
class CastObservation:
    """Contain one cast-meter measurement and its tracking evidence."""

    meter_detected: bool
    charge_ratio: float = 0.0
    confidence: float = 0.0
    bounds: tuple[int, int, int, int] | None = None
    fill_width_pixels: int | None = None
    track_width_pixels: int | None = None
    tracking_confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class BiteObservation:
    """Contain one spatially localized bite-icon observation."""

    bite_detected: bool
    confidence: float = 0.0
    bounds: tuple[int, int, int, int] | None = None
    icon_center_pixels: tuple[float, float] | None = None


@dataclass(frozen=True, slots=True)
class CatchObservation:
    """Contain conservative OCR output from a catch-result card."""

    card_detected: bool
    result_type: ResultType = ResultType.UNKNOWN
    name: str | None = None
    length_inches: int | None = None
    quantity: int = 1
    confidence: float = 0.0
    status: RecognitionStatus = RecognitionStatus.NO_TEXT
    bounds: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class EnergyObservation:
    """Contain one right-HUD energy-meter measurement."""

    meter_detected: bool
    fill_ratio: float = 0.0
    confidence: float = 0.0
    bounds: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class FoodPromptObservation:
    """Describe a confirmed English eating prompt and its Yes button."""

    prompt_detected: bool
    confidence: float = 0.0
    bounds: tuple[int, int, int, int] | None = None
    yes_center_pixels: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class TreasureLootObservation:
    """Describe a fishing-treasure item menu and transferable source slots.

    Slot centers are relative to the Stardew window so the input layer can focus
    and click the correct game window even when it moves on the desktop.
    """

    menu_detected: bool
    source_slot_centers_pixels: tuple[tuple[int, int], ...] = ()
    occupied_slot_centers_pixels: tuple[tuple[int, int], ...] = ()
    confidence: float = 0.0
    inventory_slot_count: int = 0
    occupied_inventory_slot_count: int = 0
    close_button_center_pixels: tuple[int, int] | None = None

    @property
    def inventory_full(self) -> bool:
        """Return whether every confidently located backpack slot is occupied."""
        return (
            self.inventory_slot_count > 0
            and self.occupied_inventory_slot_count >= self.inventory_slot_count
        )


@dataclass(slots=True)
class EncounterContext:
    """Track mutable timing and outcome state for the active encounter."""

    encounter_id: str
    sequence: int
    started_monotonic_seconds: float
    cast_started_monotonic_seconds: float | None = None
    bite_monotonic_seconds: float | None = None
    fight_started_monotonic_seconds: float | None = None
    cast_release_method: CastReleaseMethod | None = None
    outcome: EncounterOutcome = EncounterOutcome.ACTIVE
    result_registered: bool = False
    peak_progress_ratio: float = 0.0
    peak_progress_confidence: float = 0.0
    max_verification: MaxVerification = MaxVerification.UNKNOWN
    perfect_status: PerfectStatus = PerfectStatus.ELIGIBLE
    containment_breaks: int = 0
    minimum_margin_pixels: float | None = None
    unreliable_edge_frames: int = 0
    treasure_status: TreasureStatus = TreasureStatus.NONE
    treasure_attempts: int = 0
    treasure_collected: bool = False
    treasure_looted: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Provide an immutable, render-ready view of the application state."""

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
    start_mode: StartMode | None = None
    recording_path: str | None = None
    recorded_image_bytes: int = 0
    recording_image_limit_bytes: int = 0
    dropped_images: int = 0
    debug_warnings: int = 0
    dashboard_view: DashboardView = DashboardView.CURRENT
    historical_stats: HistoricalStatsSnapshot | None = None
    history_page: int = 0
    control_phase: ControlPhase = ControlPhase.PERFECT
    perfect_status: PerfectStatus = PerfectStatus.ELIGIBLE
    containment_margin_pixels: float | None = None
    session_perfect: int = 0
    session_perfect_attempts: int = 0
    lifetime_perfect: int = 0
    lifetime_perfect_attempts: int = 0
    treasure_status: TreasureStatus = TreasureStatus.NONE
    session_treasure_seen: int = 0
    session_treasure_collected: int = 0
    session_treasure_looted: int = 0
    lifetime_treasure_seen: int = 0
    lifetime_treasure_collected: int = 0
    lifetime_treasure_looted: int = 0
    energy_ratio: float | None = None
    energy_status: EnergyStatus = EnergyStatus.UNKNOWN
    session_food_consumed: int = 0
    lifetime_food_consumed: int = 0
    session_inventory_full_stops: int = 0
    lifetime_inventory_full_stops: int = 0


@dataclass(frozen=True, slots=True)
class ReelPilotSettings:
    """Store validated command-line settings for one ReelPilot process.

    Examples:
        >>> ReelPilotSettings(fishing_level=10).validate()
        >>> ReelPilotSettings(cast_hold_seconds=3.0).validate()
        Traceback (most recent call last):
        ...
        ValueError: cast_hold_seconds must be between 0.1 and 2.0

    """

    automation_mode: AutomationMode = AutomationMode.CONTINUOUS
    controller_profile: ControllerProfile = ControllerProfile.AUTO
    fishing_level: int | None = None
    cast_hold_seconds: float = 1.10
    cast_hold_seconds_explicit: bool = False
    plain: bool = False
    stats_enabled: bool = True
    stats_directory: Path | None = None
    record_directory: Path | None = None
    rod_slot: int = 1
    food_slot: int = 2
    auto_eat: bool = True

    def validate(self) -> None:
        """Raise ``ValueError`` when settings cannot be used safely."""
        if self.fishing_level is not None and not 0 <= self.fishing_level <= 10:
            raise ValueError("fishing_level must be between 0 and 10")
        if not 0.1 <= self.cast_hold_seconds <= 2.0:
            raise ValueError("cast_hold_seconds must be between 0.1 and 2.0")
        if (
            self.automation_mode is not AutomationMode.CONTINUOUS
            and self.cast_hold_seconds_explicit
        ):
            raise ValueError("cast_hold_seconds is only valid in continuous mode")
        if not 1 <= self.rod_slot <= 12:
            raise ValueError("rod_slot must be between 1 and 12")
        if not 1 <= self.food_slot <= 12:
            raise ValueError("food_slot must be between 1 and 12")
        if self.auto_eat and self.rod_slot == self.food_slot:
            raise ValueError("rod_slot and food_slot must differ when auto-eat is enabled")
