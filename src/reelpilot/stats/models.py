"""Immutable records exchanged by statistics services and dashboards."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import (
    AutomationMode,
    CastReleaseMethod,
    ControllerProfile,
    DifficultyTier,
    MaxVerification,
    PerfectStatus,
    RecognitionStatus,
    ResultType,
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """Describe one vanilla result that ReelPilot can recognize and enrich."""

    qualified_item_id: str
    canonical_name: str
    result_type: ResultType
    base_sell_price_gold: int | None
    fish_difficulty_score: int | None
    motion_type: str | None
    catalog_version: str

    @property
    def difficulty_tier(self) -> DifficultyTier | None:
        """Return the documented tier for the numeric difficulty score."""
        return DifficultyTier.from_score(self.fish_difficulty_score)

    def estimate_value(self, quantity: int = 1) -> tuple[int | None, int | None]:
        """Estimate vanilla base and maximum possible sale values.

        Fish maximums assume iridium quality (2x) and Angler (1.5x). ReelPilot
        cannot observe those attributes, so the upper value is only a possibility.

        Examples:
            >>> entry = CatalogEntry("(O)700", "Bullhead", ResultType.FISH, 75, 46, "smooth", "v")
            >>> entry.estimate_value()
            (75, 225)

        """
        if self.base_sell_price_gold is None:
            return None, None
        count = max(1, quantity)
        minimum = self.base_sell_price_gold * count
        maximum = (
            self.base_sell_price_gold * 3 * count
            if self.result_type is ResultType.FISH
            else minimum
        )
        return minimum, maximum


@dataclass(frozen=True, slots=True)
class CatchRecord:
    """Persist one latched catch-result event."""

    event_id: str
    encounter_id: str
    caught_at_utc: str
    result_type: ResultType
    name: str | None
    length_inches: int | None
    quantity: int
    confidence: float
    recognition_status: RecognitionStatus
    fight_milliseconds: int | None
    cast_to_result_milliseconds: int | None
    cast_release_method: CastReleaseMethod | None
    automation_mode: AutomationMode
    controller_profile: ControllerProfile
    perfect_status: PerfectStatus = PerfectStatus.UNKNOWN
    max_verification: MaxVerification = MaxVerification.UNKNOWN
    containment_breaks: int = 0
    minimum_margin_pixels: float | None = None


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    """Hold inexpensive live counters used by the control dashboard."""

    session_fish: int = 0
    session_items: int = 0
    session_escapes: int = 0
    session_casts: int = 0
    lifetime_fish: int = 0
    lifetime_items: int = 0
    lifetime_escapes: int = 0
    recent_catches: tuple[str, ...] = ()
    session_perfect: int = 0
    session_perfect_attempts: int = 0
    lifetime_perfect: int = 0
    lifetime_perfect_attempts: int = 0
    session_treasure_seen: int = 0
    session_treasure_attempts: int = 0
    session_treasure_collected: int = 0
    session_treasure_looted: int = 0
    lifetime_treasure_seen: int = 0
    lifetime_treasure_attempts: int = 0
    lifetime_treasure_collected: int = 0
    lifetime_treasure_looted: int = 0
    session_food_consumed: int = 0
    lifetime_food_consumed: int = 0
    session_inventory_full_stops: int = 0
    lifetime_inventory_full_stops: int = 0
    session_minimum_energy_ratio: float | None = None

    @property
    def session_success_ratio(self) -> float:
        """Return the caught-fish share of resolved session minigames."""
        attempts = self.session_fish + self.session_escapes
        return self.session_fish / attempts if attempts else 0.0

    @property
    def session_perfect_ratio(self) -> float:
        """Return confirmed Perfect catches divided by known Perfect outcomes."""
        return (
            self.session_perfect / self.session_perfect_attempts
            if self.session_perfect_attempts
            else 0.0
        )


@dataclass(frozen=True, slots=True)
class SpeciesStats:
    """Aggregate all recognized catches for one fish species."""

    name: str
    quantity: int
    encounter_count: int
    observed_share_ratio: float
    difficulty_tier: DifficultyTier | None
    minimum_length_inches: int | None
    average_length_inches: float | None
    maximum_length_inches: int | None
    base_sell_price_gold: int | None
    estimated_minimum_value_gold: int
    estimated_maximum_value_gold: int
    last_caught_at_utc: str


@dataclass(frozen=True, slots=True)
class SessionStats:
    """Summarize one historical ReelPilot session."""

    session_id: str
    started_at_utc: str
    runtime_milliseconds: int
    fish: int
    items: int
    escapes: int
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class HistoricalStatsSnapshot:
    """Provide a complete immutable view of lifetime statistics."""

    sessions: int = 0
    runtime_milliseconds: int = 0
    casts: int = 0
    bites: int = 0
    fish: int = 0
    fish_encounters: int = 0
    items: int = 0
    unknown_results: int = 0
    escapes: int = 0
    timeouts: int = 0
    aborted: int = 0
    estimated_minimum_value_gold: int = 0
    estimated_maximum_value_gold: int = 0
    species: tuple[SpeciesStats, ...] = ()
    recent_sessions: tuple[SessionStats, ...] = ()
    recent_catches: tuple[str, ...] = ()
    perfect_confirmed: int = 0
    perfect_missed: int = 0
    perfect_unknown: int = 0
    max_verified: int = 0
    max_attempts: int = 0
    treasure_seen: int = 0
    treasure_attempts: int = 0
    treasure_collected: int = 0
    treasure_looted: int = 0
    food_consumed: int = 0
    inventory_full_stops: int = 0
    minimum_energy_ratio: float | None = None

    @property
    def treasure_collection_ratio(self) -> float:
        """Return secured chests divided by detected chests."""
        return self.treasure_collected / self.treasure_seen if self.treasure_seen else 0.0

    @property
    def treasure_loot_ratio(self) -> float:
        """Return safely transferred menus divided by secured chests."""
        return (
            self.treasure_looted / self.treasure_collected
            if self.treasure_collected
            else 0.0
        )

    @property
    def success_ratio(self) -> float:
        """Return fish outcomes divided by fish outcomes plus escapes."""
        attempts = self.fish_encounters + self.escapes
        return self.fish_encounters / attempts if attempts else 0.0

    @property
    def perfect_ratio(self) -> float:
        """Return the verified Perfect rate for encounters with known outcomes."""
        known = self.perfect_confirmed + self.perfect_missed
        return self.perfect_confirmed / known if known else 0.0

    @property
    def max_verified_ratio(self) -> float:
        """Return OCR-verified MAX casts divided by casts with a verification row."""
        return self.max_verified / self.max_attempts if self.max_attempts else 0.0
