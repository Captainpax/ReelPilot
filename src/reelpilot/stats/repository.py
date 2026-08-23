"""SQLite persistence, schema migration, and historical statistics queries.

The repository owns a single writer thread. Live automation only enqueues immutable
commands, while reads use short-lived connections after an explicit queue barrier. This
keeps disk latency outside the 20 ms controller deadline and makes F5 refresh ordering
deterministic.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any

import cv2
import numpy as np

from ..domain import DifficultyTier
from .catalog import CATALOG_ENTRIES, CATALOG_VERSION, find_catalog_entry
from .models import (
    CatchRecord,
    HistoricalStatsSnapshot,
    SessionStats,
    SpeciesStats,
    StatsSnapshot,
)

SCHEMA_VERSION = 5


class SQLiteStatsRepository:
    """Persist statistics through one ordered background writer.

    Args:
        root: Directory containing ``reelpilot.db`` and compatibility exports.

    The class is explicitly closable and cleanup is idempotent. Call :meth:`flush`
    before a read that must include every previously queued event.

    """

    def __init__(self, root: Path) -> None:
        """Initialize or migrate the database, then start its writer thread."""
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = root / "reelpilot.db"
        self.unknown_directory = root / "unknown"
        self.unknown_directory.mkdir(exist_ok=True)
        self._initialize_database()
        self._commands: Queue[tuple[str, tuple[Any, ...]]] = Queue()
        self._stop = Event()
        self._closed = False
        self._error: str | None = None
        self._thread = Thread(target=self._writer, name="reelpilot-stats", daemon=True)
        self._thread.start()

    @property
    def error(self) -> str | None:
        """Return the first fatal writer error, if one occurred."""
        return self._error

    def begin_session(self, session_id: str, settings: dict[str, object]) -> None:
        """Queue creation of a new automation session."""
        self._enqueue(
            "session",
            session_id,
            datetime.now(UTC).isoformat(),
            json.dumps(settings, separators=(",", ":"), sort_keys=True),
        )

    def begin_encounter(self, encounter_id: str, session_id: str, sequence: int) -> None:
        """Queue creation of an encounter belonging to ``session_id``."""
        self._enqueue(
            "encounter",
            encounter_id,
            session_id,
            sequence,
            datetime.now(UTC).isoformat(),
        )

    def update_encounter(self, encounter_id: str, field: str, value: object) -> None:
        """Queue an allow-listed encounter field update.

        The allow-list prevents user-derived values from becoming SQL identifiers.
        Values remain parameterized.
        """
        allowed = {
            "cast_started_at_utc",
            "cast_release_method",
            "bite_at_utc",
            "minigame_started_at_utc",
            "ended_at_utc",
            "outcome",
            "fight_milliseconds",
            "cast_to_result_milliseconds",
            "controller_profile",
            "perfect_status",
            "containment_breaks",
            "minimum_margin_pixels",
            "max_verification",
            "treasure_status",
            "treasure_seen",
            "treasure_attempts",
            "treasure_collected",
            "treasure_looted",
        }
        if field not in allowed:
            raise ValueError(f"unsupported encounter field: {field}")
        self._enqueue("encounter-update", encounter_id, field, value)

    def update_session(self, session_id: str, field: str, value: object) -> None:
        """Queue an allow-listed low-rate session statistic update."""
        if field not in {
            "food_consumed",
            "minimum_energy_ratio",
            "inventory_full_stops",
        }:
            raise ValueError(f"unsupported session field: {field}")
        self._enqueue("session-update", session_id, field, value)

    def record_catch(self, record: CatchRecord, card: np.ndarray | None = None) -> None:
        """Queue a catch and optionally preserve an uncertain card losslessly."""
        image = card.copy() if card is not None and not record.name else None
        self._enqueue("catch", record, image)

    def close_session(
        self, session_id: str, stop_reason: str, runtime_milliseconds: int
    ) -> None:
        """Queue session closure and abort unfinished encounters in that session."""
        self._enqueue(
            "session-close",
            session_id,
            datetime.now(UTC).isoformat(),
            stop_reason,
            runtime_milliseconds,
        )

    def synchronize_catalog(self) -> None:
        """Queue an idempotent catalog upsert and historical catch backfill."""
        self._enqueue("catalog-sync")

    def flush(self, timeout_seconds: float = 2.0) -> bool:
        """Wait until every command queued before this call is committed.

        Returns:
            ``True`` when the barrier committed without a writer error; otherwise
            ``False``. The wait is bounded so a failed database cannot hang F5.

        """
        if self._closed:
            return not self._thread.is_alive() and self._error is None
        barrier = Event()
        self._enqueue("barrier", barrier)
        completed = barrier.wait(max(0.0, timeout_seconds))
        return completed and self._error is None

    def refresh_history(self, timeout_seconds: float = 2.0) -> HistoricalStatsSnapshot:
        """Synchronize metadata, flush pending writes, and query all history."""
        self.synchronize_catalog()
        if not self.flush(timeout_seconds):
            detail = f": {self._error}" if self._error else ""
            raise TimeoutError(f"statistics refresh did not complete in time{detail}")
        return self.load_history()

    def load_snapshot(self, session_id: str | None = None) -> StatsSnapshot:
        """Load compact live counters for one session and lifetime totals."""
        with closing(self._connect()) as connection:
            lifetime = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN result_type='fish' THEN quantity ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN result_type='item' THEN quantity ELSE 0 END), 0)
                FROM catches
                """
            ).fetchone()
            lifetime_escapes = connection.execute(
                "SELECT COUNT(*) FROM encounters WHERE outcome='escaped'"
            ).fetchone()[0]
            lifetime_perfect = connection.execute(
                "SELECT COUNT(*) FROM encounters WHERE perfect_status='confirmed'"
            ).fetchone()[0]
            lifetime_perfect_attempts = connection.execute(
                "SELECT COUNT(*) FROM encounters WHERE perfect_status IN ('confirmed','missed')"
            ).fetchone()[0]
            lifetime_treasure = connection.execute(
                """
                SELECT COALESCE(SUM(treasure_seen),0),
                       COALESCE(SUM(treasure_attempts),0),
                       COALESCE(SUM(treasure_collected),0),
                       COALESCE(SUM(treasure_looted),0)
                FROM encounters
                """
            ).fetchone()
            lifetime_energy = connection.execute(
                """
                SELECT COALESCE(SUM(food_consumed),0),
                       COALESCE(SUM(inventory_full_stops),0)
                FROM sessions
                """
            ).fetchone()
            if session_id is None:
                session_values = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
                session_energy = (0, 0, None)
            else:
                session_values = connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN c.result_type='fish' THEN c.quantity ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN c.result_type='item' THEN c.quantity ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN e.outcome='escaped' THEN 1 ELSE 0 END), 0),
                        COUNT(DISTINCT e.encounter_id),
                        COALESCE(SUM(CASE WHEN e.perfect_status='confirmed' THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN e.perfect_status IN ('confirmed','missed') THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(e.treasure_seen),0),
                        COALESCE(SUM(e.treasure_attempts),0),
                        COALESCE(SUM(e.treasure_collected),0),
                        COALESCE(SUM(e.treasure_looted),0)
                    FROM encounters e
                    LEFT JOIN catches c ON c.encounter_id=e.encounter_id
                    WHERE e.session_id=?
                    """,
                    (session_id,),
                ).fetchone()
                session_energy = connection.execute(
                    """
                    SELECT food_consumed,inventory_full_stops,minimum_energy_ratio
                    FROM sessions WHERE session_id=?
                    """,
                    (session_id,),
                ).fetchone() or (0, 0, None)
            recent = connection.execute(
                """
                SELECT COALESCE(name, 'Unknown'), length_inches, quantity
                FROM catches ORDER BY caught_at_utc DESC LIMIT 8
                """
            ).fetchall()
        recent_labels = tuple(
            f"{name}{f' — {length} in.' if length is not None else ''}{f' ×{quantity}' if quantity > 1 else ''}"
            for name, length, quantity in recent
        )
        return StatsSnapshot(
            session_fish=int(session_values[0]),
            session_items=int(session_values[1]),
            session_escapes=int(session_values[2]),
            session_casts=int(session_values[3]),
            lifetime_fish=int(lifetime[0]),
            lifetime_items=int(lifetime[1]),
            lifetime_escapes=int(lifetime_escapes),
            recent_catches=recent_labels,
            session_perfect=int(session_values[4]),
            session_perfect_attempts=int(session_values[5]),
            lifetime_perfect=int(lifetime_perfect),
            lifetime_perfect_attempts=int(lifetime_perfect_attempts),
            session_treasure_seen=int(session_values[6]),
            session_treasure_attempts=int(session_values[7]),
            session_treasure_collected=int(session_values[8]),
            session_treasure_looted=int(session_values[9]),
            lifetime_treasure_seen=int(lifetime_treasure[0]),
            lifetime_treasure_attempts=int(lifetime_treasure[1]),
            lifetime_treasure_collected=int(lifetime_treasure[2]),
            lifetime_treasure_looted=int(lifetime_treasure[3]),
            session_food_consumed=int(session_energy[0]),
            lifetime_food_consumed=int(lifetime_energy[0]),
            session_inventory_full_stops=int(session_energy[1]),
            lifetime_inventory_full_stops=int(lifetime_energy[1]),
            session_minimum_energy_ratio=(
                float(session_energy[2]) if session_energy[2] is not None else None
            ),
        )

    def load_history(self) -> HistoricalStatsSnapshot:
        """Query aggregate lifetime, session, species, rarity, and value statistics."""
        with closing(self._connect()) as connection:
            session_totals = connection.execute(
                """
                SELECT COUNT(*),COALESCE(SUM(runtime_milliseconds),0),
                       COALESCE(SUM(food_consumed),0),
                       COALESCE(SUM(inventory_full_stops),0),
                       MIN(minimum_energy_ratio)
                FROM sessions
                """
            ).fetchone()
            encounter_totals = connection.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN bite_at_utc IS NOT NULL THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN outcome='escaped' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN outcome='timed-out' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN outcome='aborted' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN perfect_status='confirmed' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN perfect_status='missed' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN perfect_status IN ('unknown','eligible') THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN max_verification='verified' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN max_verification IS NOT NULL THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(treasure_seen),0),
                       COALESCE(SUM(treasure_attempts),0),
                       COALESCE(SUM(treasure_collected),0),
                       COALESCE(SUM(treasure_looted),0)
                FROM encounters
                """
            ).fetchone()
            catch_totals = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN result_type='fish' THEN quantity ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN result_type='item' THEN quantity ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN result_type='unknown' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(estimated_minimum_value_gold), 0),
                    COALESCE(SUM(estimated_maximum_value_gold), 0),
                    COUNT(DISTINCT CASE WHEN result_type='fish' THEN encounter_id END)
                FROM catches
                """
            ).fetchone()
            total_fish = int(catch_totals[0])
            species_rows = connection.execute(
                """
                SELECT c.name, SUM(c.quantity), COUNT(DISTINCT c.encounter_id),
                       MAX(c.fish_difficulty_score), MIN(c.length_inches),
                       SUM(CASE WHEN c.length_inches IS NOT NULL THEN c.length_inches*c.quantity ELSE 0 END)*1.0 /
                         NULLIF(SUM(CASE WHEN c.length_inches IS NOT NULL THEN c.quantity ELSE 0 END), 0),
                       MAX(c.length_inches), MAX(c.base_sell_price_gold),
                       COALESCE(SUM(c.estimated_minimum_value_gold), 0),
                       COALESCE(SUM(c.estimated_maximum_value_gold), 0),
                       MAX(c.caught_at_utc)
                FROM catches c
                WHERE c.result_type='fish' AND c.name IS NOT NULL
                GROUP BY c.name COLLATE NOCASE
                ORDER BY SUM(c.quantity) DESC, c.name COLLATE NOCASE
                """
            ).fetchall()
            recent_session_rows = connection.execute(
                """
                SELECT s.session_id, s.started_at_utc, COALESCE(s.runtime_milliseconds, 0),
                       COALESCE((SELECT SUM(c.quantity) FROM catches c JOIN encounters e ON e.encounter_id=c.encounter_id WHERE e.session_id=s.session_id AND c.result_type='fish'), 0),
                       COALESCE((SELECT SUM(c.quantity) FROM catches c JOIN encounters e ON e.encounter_id=c.encounter_id WHERE e.session_id=s.session_id AND c.result_type='item'), 0),
                       COALESCE((SELECT COUNT(*) FROM encounters e WHERE e.session_id=s.session_id AND e.outcome='escaped'), 0),
                       s.stop_reason
                FROM sessions s ORDER BY s.started_at_utc DESC LIMIT 8
                """
            ).fetchall()
            recent = connection.execute(
                """
                SELECT COALESCE(name, 'Unknown'), length_inches, quantity,
                       estimated_minimum_value_gold, estimated_maximum_value_gold
                FROM catches ORDER BY caught_at_utc DESC LIMIT 12
                """
            ).fetchall()

        species = tuple(
            SpeciesStats(
                name=str(row[0]),
                quantity=int(row[1]),
                encounter_count=int(row[2]),
                observed_share_ratio=(int(row[1]) / total_fish if total_fish else 0.0),
                difficulty_tier=DifficultyTier.from_score(
                    int(row[3]) if row[3] is not None else None
                ),
                minimum_length_inches=int(row[4]) if row[4] is not None else None,
                average_length_inches=float(row[5]) if row[5] is not None else None,
                maximum_length_inches=int(row[6]) if row[6] is not None else None,
                base_sell_price_gold=int(row[7]) if row[7] is not None else None,
                estimated_minimum_value_gold=int(row[8]),
                estimated_maximum_value_gold=int(row[9]),
                last_caught_at_utc=str(row[10]),
            )
            for row in species_rows
        )
        sessions = tuple(
            SessionStats(
                str(row[0]), str(row[1]), int(row[2]), int(row[3]), int(row[4]),
                int(row[5]), str(row[6]) if row[6] is not None else None,
            )
            for row in recent_session_rows
        )
        recent_labels = tuple(
            _catch_label(name, length, quantity, minimum, maximum)
            for name, length, quantity, minimum, maximum in recent
        )
        return HistoricalStatsSnapshot(
            sessions=int(session_totals[0]),
            runtime_milliseconds=int(session_totals[1]),
            casts=int(encounter_totals[0]),
            bites=int(encounter_totals[1]),
            fish=total_fish,
            fish_encounters=int(catch_totals[5]),
            items=int(catch_totals[1]),
            unknown_results=int(catch_totals[2]),
            escapes=int(encounter_totals[2]),
            timeouts=int(encounter_totals[3]),
            aborted=int(encounter_totals[4]),
            estimated_minimum_value_gold=int(catch_totals[3]),
            estimated_maximum_value_gold=int(catch_totals[4]),
            species=species,
            recent_sessions=sessions,
            recent_catches=recent_labels,
            perfect_confirmed=int(encounter_totals[5]),
            perfect_missed=int(encounter_totals[6]),
            perfect_unknown=int(encounter_totals[7]),
            max_verified=int(encounter_totals[8]),
            max_attempts=int(encounter_totals[9]),
            treasure_seen=int(encounter_totals[10]),
            treasure_attempts=int(encounter_totals[11]),
            treasure_collected=int(encounter_totals[12]),
            treasure_looted=int(encounter_totals[13]),
            food_consumed=int(session_totals[2]),
            inventory_full_stops=int(session_totals[3]),
            minimum_energy_ratio=(
                float(session_totals[4]) if session_totals[4] is not None else None
            ),
        )

    def close(self, timeout_seconds: float = 2.0) -> None:
        """Flush queued work, stop the writer, and write atomic compatibility exports."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=max(0.0, timeout_seconds))
        if not self._thread.is_alive() and self._error is None:
            self.export_compatibility_files()

    def export_compatibility_files(self) -> None:
        """Atomically regenerate human-readable CSV and JSON exports."""
        catches_csv = self.root / "catches.csv"
        summary_json = self.root / "summary.json"
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                SELECT c.event_id,c.caught_at_utc,c.result_type,c.name,c.length_inches,
                       c.quantity,c.confidence,c.recognition_status,c.fight_milliseconds,
                       c.cast_to_result_milliseconds,c.cast_release_method,
                       c.automation_mode,c.controller_profile,c.qualified_item_id,
                       c.base_sell_price_gold,c.fish_difficulty_score,
                       c.estimated_minimum_value_gold,c.estimated_maximum_value_gold,
                       e.perfect_status,e.containment_breaks,e.minimum_margin_pixels,
                       e.max_verification,e.treasure_status,e.treasure_seen,
                       e.treasure_attempts,e.treasure_collected,e.treasure_looted
                FROM catches c JOIN encounters e ON e.encounter_id=c.encounter_id
                ORDER BY c.caught_at_utc
                """
            )
            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description]
        temporary_csv = catches_csv.with_suffix(".csv.tmp")
        with temporary_csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)
        temporary_csv.replace(catches_csv)

        history = self.load_history()
        summary = {
            "schema_version": SCHEMA_VERSION,
            "catalog_version": CATALOG_VERSION,
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "performance": {
                "sessions": history.sessions,
                "runtime_milliseconds": history.runtime_milliseconds,
                "casts": history.casts,
                "bites": history.bites,
                "fish_caught": history.fish,
                "fish_encounters": history.fish_encounters,
                "items_caught": history.items,
                "unknown_results": history.unknown_results,
                "escapes": history.escapes,
                "timeouts": history.timeouts,
                "aborted": history.aborted,
                "success_ratio": history.success_ratio,
                "perfect_confirmed": history.perfect_confirmed,
                "perfect_missed": history.perfect_missed,
                "perfect_unknown": history.perfect_unknown,
                "perfect_ratio": history.perfect_ratio,
                "max_verified": history.max_verified,
                "max_attempts": history.max_attempts,
                "max_verified_ratio": history.max_verified_ratio,
                "treasure_seen": history.treasure_seen,
                "treasure_attempts": history.treasure_attempts,
                "treasure_collected": history.treasure_collected,
                "treasure_looted": history.treasure_looted,
                "treasure_collection_ratio": history.treasure_collection_ratio,
                "treasure_loot_ratio": history.treasure_loot_ratio,
                "food_consumed": history.food_consumed,
                "inventory_full_stops": history.inventory_full_stops,
                "minimum_energy_ratio": history.minimum_energy_ratio,
                "estimated_value_gold": {
                    "base": history.estimated_minimum_value_gold,
                    "possible_maximum": history.estimated_maximum_value_gold,
                },
            },
            "species": {
                row.name: {
                    "quantity": row.quantity,
                    "encounters": row.encounter_count,
                    "observed_share_ratio": row.observed_share_ratio,
                    "difficulty": row.difficulty_tier.value if row.difficulty_tier else None,
                    "shortest_length_inches": row.minimum_length_inches,
                    "longest_length_inches": row.maximum_length_inches,
                    "average_length_inches": row.average_length_inches,
                    "base_sell_price_gold": row.base_sell_price_gold,
                    "estimated_value_gold": {
                        "base": row.estimated_minimum_value_gold,
                        "possible_maximum": row.estimated_maximum_value_gold,
                    },
                }
                for row in history.species
            },
        }
        temporary_json = summary_json.with_suffix(".json.tmp")
        temporary_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        temporary_json.replace(summary_json)

    def _enqueue(self, operation: str, *payload: Any) -> None:
        """Add an operation unless shutdown has already begun."""
        if not self._closed:
            self._commands.put_nowait((operation, payload))

    def _writer(self) -> None:
        """Commit queued commands in small transactions on the writer thread."""
        pending_barriers: list[Event] = []
        try:
            with closing(self._connect()) as connection:
                while not self._stop.is_set() or not self._commands.empty():
                    batch: list[tuple[str, tuple[Any, ...]]] = []
                    try:
                        batch.append(self._commands.get(timeout=0.05))
                    except Empty:
                        continue
                    while len(batch) < 50:
                        try:
                            batch.append(self._commands.get_nowait())
                        except Empty:
                            break
                    pending_barriers = [
                        payload[0] for operation, payload in batch if operation == "barrier"
                    ]
                    try:
                        connection.execute("BEGIN")
                        for operation, payload in batch:
                            self._execute(connection, operation, payload)
                        connection.commit()
                        for barrier in pending_barriers:
                            barrier.set()
                        pending_barriers = []
                    except Exception:
                        connection.rollback()
                        raise
                    finally:
                        for _ in batch:
                            self._commands.task_done()
        except Exception as exc:
            self._error = str(exc)
            for barrier in pending_barriers:
                barrier.set()
            while True:
                try:
                    operation, payload = self._commands.get_nowait()
                except Empty:
                    break
                if operation == "barrier":
                    payload[0].set()
                self._commands.task_done()

    def _execute(
        self,
        connection: sqlite3.Connection,
        operation: str,
        payload: tuple[Any, ...],
    ) -> None:
        """Apply one trusted writer operation inside the current transaction."""
        if operation == "session":
            connection.execute(
                "INSERT INTO sessions(session_id,started_at_utc,settings_json) VALUES(?,?,?)",
                payload,
            )
        elif operation == "encounter":
            connection.execute(
                "INSERT INTO encounters(encounter_id,session_id,sequence,started_at_utc) VALUES(?,?,?,?)",
                payload,
            )
        elif operation == "encounter-update":
            encounter_id, field, value = payload
            connection.execute(
                f"UPDATE encounters SET {field}=? WHERE encounter_id=?", (value, encounter_id)
            )
        elif operation == "session-update":
            session_id, field, value = payload
            connection.execute(
                f"UPDATE sessions SET {field}=? WHERE session_id=?", (value, session_id)
            )
        elif operation == "catch":
            self._insert_catch(connection, payload[0], payload[1])
        elif operation == "session-close":
            session_id, ended_at, reason, runtime = payload
            connection.execute(
                "UPDATE sessions SET ended_at_utc=?,stop_reason=?,runtime_milliseconds=? WHERE session_id=?",
                (ended_at, reason, runtime, session_id),
            )
            connection.execute(
                "UPDATE encounters SET outcome='aborted',ended_at_utc=COALESCE(ended_at_utc,?) WHERE session_id=? AND outcome='active'",
                (ended_at, session_id),
            )
        elif operation == "catalog-sync":
            self._synchronize_catalog(connection)
        elif operation != "barrier":
            raise ValueError(f"unsupported statistics operation: {operation}")

    def _insert_catch(
        self, connection: sqlite3.Connection, record: CatchRecord, card: np.ndarray | None
    ) -> None:
        """Insert one catch with catalog values snapped at recognition time."""
        card_path = None
        if card is not None:
            path = self.unknown_directory / f"{record.event_id}.png"
            if cv2.imwrite(str(path), card):
                card_path = str(path)
        catalog = find_catalog_entry(record.name)
        minimum, maximum = catalog.estimate_value(record.quantity) if catalog else (None, None)
        connection.execute(
            """
            INSERT OR IGNORE INTO catches(
                event_id,encounter_id,caught_at_utc,result_type,name,length_inches,
                quantity,confidence,recognition_status,fight_milliseconds,
                cast_to_result_milliseconds,cast_release_method,automation_mode,
                controller_profile,card_path,schema_version,qualified_item_id,
                catalog_version,base_sell_price_gold,fish_difficulty_score,
                estimated_minimum_value_gold,estimated_maximum_value_gold
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.event_id, record.encounter_id, record.caught_at_utc,
                record.result_type.value, record.name, record.length_inches,
                max(1, record.quantity), record.confidence, record.recognition_status.value,
                record.fight_milliseconds, record.cast_to_result_milliseconds,
                record.cast_release_method.value if record.cast_release_method else None,
                record.automation_mode.value, record.controller_profile.value, card_path,
                SCHEMA_VERSION, catalog.qualified_item_id if catalog else None,
                catalog.catalog_version if catalog else None,
                catalog.base_sell_price_gold if catalog else None,
                catalog.fish_difficulty_score if catalog else None, minimum, maximum,
            ),
        )

    def _initialize_database(self) -> None:
        """Create the base schema, migrate v1 data, and reconcile stale state."""
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations(
                    version INTEGER PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions(
                    session_id TEXT PRIMARY KEY,
                    started_at_utc TEXT NOT NULL,
                    ended_at_utc TEXT,
                    runtime_milliseconds INTEGER,
                    settings_json TEXT NOT NULL,
                    stop_reason TEXT,
                    food_consumed INTEGER NOT NULL DEFAULT 0,
                    minimum_energy_ratio REAL,
                    inventory_full_stops INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS encounters(
                    encounter_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    sequence INTEGER NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    cast_started_at_utc TEXT,
                    cast_release_method TEXT,
                    bite_at_utc TEXT,
                    minigame_started_at_utc TEXT,
                    ended_at_utc TEXT,
                    outcome TEXT NOT NULL DEFAULT 'active',
                    fight_milliseconds INTEGER,
                    cast_to_result_milliseconds INTEGER,
                    controller_profile TEXT
                    ,perfect_status TEXT
                    ,containment_breaks INTEGER NOT NULL DEFAULT 0
                    ,minimum_margin_pixels REAL
                    ,max_verification TEXT
                    ,treasure_status TEXT NOT NULL DEFAULT 'none'
                    ,treasure_seen INTEGER NOT NULL DEFAULT 0
                    ,treasure_attempts INTEGER NOT NULL DEFAULT 0
                    ,treasure_collected INTEGER NOT NULL DEFAULT 0
                    ,treasure_looted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS catches(
                    event_id TEXT PRIMARY KEY,
                    encounter_id TEXT NOT NULL UNIQUE REFERENCES encounters(encounter_id),
                    caught_at_utc TEXT NOT NULL,
                    result_type TEXT NOT NULL CHECK(result_type IN ('fish','item','unknown')),
                    name TEXT,
                    length_inches INTEGER,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    confidence REAL NOT NULL,
                    recognition_status TEXT NOT NULL,
                    fight_milliseconds INTEGER,
                    cast_to_result_milliseconds INTEGER,
                    cast_release_method TEXT,
                    automation_mode TEXT NOT NULL,
                    controller_profile TEXT NOT NULL,
                    card_path TEXT,
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_items(
                    qualified_item_id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    result_type TEXT NOT NULL,
                    base_sell_price_gold INTEGER,
                    fish_difficulty_score INTEGER,
                    motion_type TEXT,
                    catalog_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_encounters_session ON encounters(session_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_catches_time ON catches(caught_at_utc DESC);
                CREATE INDEX IF NOT EXISTS idx_catches_species ON catches(result_type, name);
                """
            )
            self._add_catch_columns(connection)
            self._add_encounter_columns(connection)
            self._add_session_columns(connection)
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "UPDATE sessions SET stop_reason='unclean-shutdown', ended_at_utc=? WHERE ended_at_utc IS NULL",
                (now,),
            )
            connection.execute(
                """
                UPDATE encounters
                SET outcome='aborted',
                    ended_at_utc=COALESCE(ended_at_utc,(
                        SELECT ended_at_utc FROM sessions WHERE sessions.session_id=encounters.session_id
                    ))
                WHERE outcome='active' AND EXISTS(
                    SELECT 1 FROM sessions
                    WHERE sessions.session_id=encounters.session_id
                      AND sessions.ended_at_utc IS NOT NULL
                )
                """
            )
            self._synchronize_catalog(connection)
            connection.executemany(
                "INSERT OR IGNORE INTO schema_migrations VALUES(?,?)",
                ((2, now), (3, now), (4, now), (SCHEMA_VERSION, now)),
            )
            connection.commit()

    @staticmethod
    def _add_catch_columns(connection: sqlite3.Connection) -> None:
        """Add schema-v2 enrichment columns when upgrading a v1 database."""
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(catches)").fetchall()
        }
        additions = {
            "qualified_item_id": "TEXT",
            "catalog_version": "TEXT",
            "base_sell_price_gold": "INTEGER",
            "fish_difficulty_score": "INTEGER",
            "estimated_minimum_value_gold": "INTEGER",
            "estimated_maximum_value_gold": "INTEGER",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE catches ADD COLUMN {name} {sql_type}")

    @staticmethod
    def _add_encounter_columns(connection: sqlite3.Connection) -> None:
        """Add schema-v3 Perfect and MAX evidence to existing databases."""
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(encounters)").fetchall()
        }
        additions = {
            "perfect_status": "TEXT",
            "containment_breaks": "INTEGER NOT NULL DEFAULT 0",
            "minimum_margin_pixels": "REAL",
            "max_verification": "TEXT",
            "treasure_status": "TEXT NOT NULL DEFAULT 'none'",
            "treasure_seen": "INTEGER NOT NULL DEFAULT 0",
            "treasure_attempts": "INTEGER NOT NULL DEFAULT 0",
            "treasure_collected": "INTEGER NOT NULL DEFAULT 0",
            "treasure_looted": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE encounters ADD COLUMN {name} {sql_type}"
                )

    @staticmethod
    def _add_session_columns(connection: sqlite3.Connection) -> None:
        """Add schema-v5 refueling and inventory-stop statistics."""
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        additions = {
            "food_consumed": "INTEGER NOT NULL DEFAULT 0",
            "minimum_energy_ratio": "REAL",
            "inventory_full_stops": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {sql_type}")

    @staticmethod
    def _synchronize_catalog(connection: sqlite3.Connection) -> None:
        """Upsert the catalog and enrich only confidently named catches."""
        connection.executemany(
            """
            INSERT INTO catalog_items VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(qualified_item_id) DO UPDATE SET
                canonical_name=excluded.canonical_name,
                result_type=excluded.result_type,
                base_sell_price_gold=excluded.base_sell_price_gold,
                fish_difficulty_score=excluded.fish_difficulty_score,
                motion_type=excluded.motion_type,
                catalog_version=excluded.catalog_version
            """,
            (
                (
                    entry.qualified_item_id, entry.canonical_name, entry.result_type.value,
                    entry.base_sell_price_gold, entry.fish_difficulty_score,
                    entry.motion_type, entry.catalog_version,
                )
                for entry in CATALOG_ENTRIES
            ),
        )
        connection.execute(
            """
            UPDATE catches
            SET qualified_item_id=(SELECT qualified_item_id FROM catalog_items WHERE canonical_name=catches.name COLLATE NOCASE),
                catalog_version=(SELECT catalog_version FROM catalog_items WHERE canonical_name=catches.name COLLATE NOCASE),
                base_sell_price_gold=(SELECT base_sell_price_gold FROM catalog_items WHERE canonical_name=catches.name COLLATE NOCASE),
                fish_difficulty_score=(SELECT fish_difficulty_score FROM catalog_items WHERE canonical_name=catches.name COLLATE NOCASE),
                estimated_minimum_value_gold=(SELECT base_sell_price_gold*catches.quantity FROM catalog_items WHERE canonical_name=catches.name COLLATE NOCASE),
                estimated_maximum_value_gold=(SELECT base_sell_price_gold*catches.quantity*CASE WHEN result_type='fish' THEN 3 ELSE 1 END FROM catalog_items WHERE canonical_name=catches.name COLLATE NOCASE)
            WHERE recognition_status='recognized' AND name IS NOT NULL
              AND EXISTS(SELECT 1 FROM catalog_items WHERE canonical_name=catches.name COLLATE NOCASE)
            """
        )

    def _connect(self) -> sqlite3.Connection:
        """Open a consistently configured SQLite connection."""
        connection = sqlite3.connect(self.database_path, timeout=2.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        return connection


def _catch_label(
    name: Any,
    length: Any,
    quantity: Any,
    minimum: Any,
    maximum: Any,
) -> str:
    """Format one historical catch without inventing unavailable values."""
    label = str(name)
    if length is not None:
        label += f" — {int(length)} in."
    if int(quantity) > 1:
        label += f" ×{int(quantity)}"
    if minimum is not None and maximum is not None:
        label += (
            f" — {int(minimum)}g"
            if int(minimum) == int(maximum)
            else f" — {int(minimum)}–{int(maximum)}g est."
        )
    return label
