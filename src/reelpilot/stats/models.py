from __future__ import annotations

from dataclasses import dataclass

from ..domain import (
    AutomationMode,
    CastReleaseMethod,
    ControllerProfile,
    RecognitionStatus,
    ResultType,
)


@dataclass(frozen=True, slots=True)
class CatchRecord:
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


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    session_fish: int = 0
    session_items: int = 0
    session_escapes: int = 0
    session_casts: int = 0
    lifetime_fish: int = 0
    lifetime_items: int = 0
    lifetime_escapes: int = 0
    recent_catches: tuple[str, ...] = ()

    @property
    def session_success_ratio(self) -> float:
        attempts = self.session_fish + self.session_escapes
        return self.session_fish / attempts if attempts else 0.0
