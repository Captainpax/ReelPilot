from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock

import numpy as np

from ..domain import EncounterOutcome
from .models import CatchRecord, StatsSnapshot
from .repository import SQLiteStatsRepository


class StatsService:
    def __init__(
        self,
        repository: SQLiteStatsRepository,
        session_id: str,
        settings: dict[str, object],
    ) -> None:
        self.repository = repository
        self.session_id = session_id
        self._lock = Lock()
        self._snapshot = repository.load_snapshot(session_id)
        repository.begin_session(session_id, settings)

    @property
    def snapshot(self) -> StatsSnapshot:
        with self._lock:
            return self._snapshot

    def begin_encounter(self, encounter_id: str, sequence: int) -> None:
        self.repository.begin_encounter(encounter_id, self.session_id, sequence)
        with self._lock:
            self._snapshot = replace(
                self._snapshot, session_casts=self._snapshot.session_casts + 1
            )

    def record_escape(self, encounter_id: str) -> None:
        self.repository.update_encounter(
            encounter_id, "outcome", EncounterOutcome.ESCAPED.value
        )
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                session_escapes=self._snapshot.session_escapes + 1,
                lifetime_escapes=self._snapshot.lifetime_escapes + 1,
            )

    def record_catch(self, record: CatchRecord, card: np.ndarray | None = None) -> None:
        self.repository.record_catch(record, card)
        label = record.name or "Unknown result"
        if record.length_inches is not None:
            label += f" — {record.length_inches} in."
        with self._lock:
            if record.result_type.value == "fish":
                self._snapshot = replace(
                    self._snapshot,
                    session_fish=self._snapshot.session_fish + record.quantity,
                    lifetime_fish=self._snapshot.lifetime_fish + record.quantity,
                    recent_catches=(label, *self._snapshot.recent_catches[:7]),
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
        return uuid.uuid4().hex

    @staticmethod
    def now_utc() -> str:
        return datetime.now(UTC).isoformat()
