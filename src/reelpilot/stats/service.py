"""Thread-safe application service for live counters and historical refreshes."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock

import numpy as np

from ..domain import EncounterOutcome, PerfectStatus
from .models import CatchRecord, HistoricalStatsSnapshot, StatsSnapshot
from .repository import SQLiteStatsRepository


class StatsService:
    """Coordinate one live session with the asynchronous SQLite repository."""

    def __init__(
        self,
        repository: SQLiteStatsRepository,
        session_id: str,
        settings: dict[str, object],
    ) -> None:
        """Begin ``session_id`` and initialize its immediate live counters."""
        self.repository = repository
        self.session_id = session_id
        self._lock = Lock()
        self._snapshot = repository.load_snapshot(session_id)
        repository.begin_session(session_id, settings)

    @property
    def snapshot(self) -> StatsSnapshot:
        """Return the latest immutable live counter snapshot."""
        with self._lock:
            return self._snapshot

    def refresh_history(self, timeout_seconds: float = 2.0) -> HistoricalStatsSnapshot:
        """Commit pending events and return an all-time historical snapshot."""
        history = self.repository.refresh_history(timeout_seconds)
        with self._lock:
            self._snapshot = self.repository.load_snapshot(self.session_id)
        return history

    def begin_encounter(self, encounter_id: str, sequence: int) -> None:
        """Record a new cast/encounter and update the immediate session count."""
        self.repository.begin_encounter(encounter_id, self.session_id, sequence)
        with self._lock:
            self._snapshot = replace(
                self._snapshot, session_casts=self._snapshot.session_casts + 1
            )

    def record_escape(self, encounter_id: str) -> None:
        """Record a minigame escape exactly once."""
        self.repository.update_encounter(
            encounter_id, "outcome", EncounterOutcome.ESCAPED.value
        )
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                session_escapes=self._snapshot.session_escapes + 1,
                lifetime_escapes=self._snapshot.lifetime_escapes + 1,
            )

    def record_treasure_seen(self) -> None:
        """Increment detected-chest counters after the engine's per-encounter latch."""
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                session_treasure_seen=self._snapshot.session_treasure_seen + 1,
                lifetime_treasure_seen=self._snapshot.lifetime_treasure_seen + 1,
            )

    def record_treasure_attempt(self) -> None:
        """Increment bounded treasure-attempt counters."""
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                session_treasure_attempts=self._snapshot.session_treasure_attempts + 1,
                lifetime_treasure_attempts=self._snapshot.lifetime_treasure_attempts + 1,
            )

    def record_treasure_collected(self) -> None:
        """Increment secured-chest counters after disappearance confirmation."""
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                session_treasure_collected=self._snapshot.session_treasure_collected + 1,
                lifetime_treasure_collected=self._snapshot.lifetime_treasure_collected + 1,
            )

    def record_treasure_looted(self) -> None:
        """Increment counters after every visible loot stack transfers safely."""
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                session_treasure_looted=self._snapshot.session_treasure_looted + 1,
                lifetime_treasure_looted=self._snapshot.lifetime_treasure_looted + 1,
            )

    def record_energy(self, energy_ratio: float) -> None:
        """Persist the lowest reliable energy ratio observed this session."""
        ratio = min(1.0, max(0.0, energy_ratio))
        with self._lock:
            previous = self._snapshot.session_minimum_energy_ratio
            minimum = ratio if previous is None else min(previous, ratio)
            if previous is not None and minimum == previous:
                return
            self._snapshot = replace(
                self._snapshot, session_minimum_energy_ratio=minimum
            )
        self.repository.update_session(self.session_id, "minimum_energy_ratio", minimum)

    def record_food_consumed(self) -> None:
        """Increment durable and live counters after verified energy gain."""
        with self._lock:
            session_count = self._snapshot.session_food_consumed + 1
            self._snapshot = replace(
                self._snapshot,
                session_food_consumed=session_count,
                lifetime_food_consumed=self._snapshot.lifetime_food_consumed + 1,
            )
        self.repository.update_session(self.session_id, "food_consumed", session_count)

    def record_inventory_full_stop(self) -> None:
        """Increment counters when an item-grab menu cannot transfer."""
        with self._lock:
            session_count = self._snapshot.session_inventory_full_stops + 1
            self._snapshot = replace(
                self._snapshot,
                session_inventory_full_stops=session_count,
                lifetime_inventory_full_stops=(
                    self._snapshot.lifetime_inventory_full_stops + 1
                ),
            )
        self.repository.update_session(
            self.session_id, "inventory_full_stops", session_count
        )

    def record_catch(self, record: CatchRecord, card: np.ndarray | None = None) -> None:
        """Record one latched catch and update immediate counters."""
        self.repository.record_catch(record, card)
        label = record.name or "Unknown result"
        if record.length_inches is not None:
            label += f" — {record.length_inches} in."
        with self._lock:
            perfect_increment = int(record.perfect_status is PerfectStatus.CONFIRMED)
            perfect_attempt_increment = int(
                record.perfect_status
                in {PerfectStatus.CONFIRMED, PerfectStatus.MISSED}
            )
            if record.result_type.value == "fish":
                self._snapshot = replace(
                    self._snapshot,
                    session_fish=self._snapshot.session_fish + record.quantity,
                    lifetime_fish=self._snapshot.lifetime_fish + record.quantity,
                    recent_catches=(label, *self._snapshot.recent_catches[:7]),
                    session_perfect=self._snapshot.session_perfect
                    + perfect_increment,
                    session_perfect_attempts=self._snapshot.session_perfect_attempts
                    + perfect_attempt_increment,
                    lifetime_perfect=self._snapshot.lifetime_perfect
                    + perfect_increment,
                    lifetime_perfect_attempts=self._snapshot.lifetime_perfect_attempts
                    + perfect_attempt_increment,
                )
            elif record.result_type.value == "item":
                self._snapshot = replace(
                    self._snapshot,
                    session_items=self._snapshot.session_items + record.quantity,
                    lifetime_items=self._snapshot.lifetime_items + record.quantity,
                    recent_catches=(label, *self._snapshot.recent_catches[:7]),
                )

    @staticmethod
    def new_event_id() -> str:
        """Create a stable random identifier for one catch event."""
        return uuid.uuid4().hex

    @staticmethod
    def now_utc() -> str:
        """Return an ISO-8601 UTC timestamp suitable for SQLite and JSON."""
        return datetime.now(UTC).isoformat()
