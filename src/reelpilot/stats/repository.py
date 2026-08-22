from __future__ import annotations

import csv
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any

import cv2
import numpy as np

from .models import CatchRecord, StatsSnapshot

SCHEMA_VERSION = 1


class SQLiteStatsRepository:
    """Single-writer SQLite repository; callers never perform control-loop I/O."""

    def __init__(self, root: Path) -> None:
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
        return self._error

    def begin_session(self, session_id: str, settings: dict[str, object]) -> None:
        self._enqueue(
            "session",
            session_id,
            datetime.now(UTC).isoformat(),
            json.dumps(settings, separators=(",", ":"), sort_keys=True),
        )

    def begin_encounter(self, encounter_id: str, session_id: str, sequence: int) -> None:
        self._enqueue(
            "encounter",
            encounter_id,
            session_id,
            sequence,
            datetime.now(UTC).isoformat(),
        )

    def update_encounter(self, encounter_id: str, field: str, value: object) -> None:
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
        }
        if field not in allowed:
            raise ValueError(f"unsupported encounter field: {field}")
        self._enqueue("encounter-update", encounter_id, field, value)

    def record_catch(self, record: CatchRecord, card: np.ndarray | None = None) -> None:
        image = card.copy() if card is not None and not record.name else None
        self._enqueue("catch", record, image)

    def load_snapshot(self, session_id: str | None = None) -> StatsSnapshot:
        with self._connect() as connection:
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
            if session_id is None:
                session_values = (0, 0, 0, 0)
            else:
                session_values = connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN c.result_type='fish' THEN c.quantity ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN c.result_type='item' THEN c.quantity ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN e.outcome='escaped' THEN 1 ELSE 0 END), 0),
                        COUNT(DISTINCT e.encounter_id)
                    FROM encounters e
                    LEFT JOIN catches c ON c.encounter_id=e.encounter_id
                    WHERE e.session_id=?
                    """,
                    (session_id,),
                ).fetchone()
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
            int(session_values[0]),
            int(session_values[1]),
            int(session_values[2]),
            int(session_values[3]),
            int(lifetime[0]),
            int(lifetime[1]),
            int(lifetime_escapes),
            recent_labels,
        )

    def close_session(
        self, session_id: str, stop_reason: str, runtime_milliseconds: int
    ) -> None:
        self._enqueue(
            "session-close",
            session_id,
            datetime.now(UTC).isoformat(),
            stop_reason,
            runtime_milliseconds,
        )

    def close(self, timeout_seconds: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=max(0.0, timeout_seconds))
        if not self._thread.is_alive():
            self.export_compatibility_files()

    def export_compatibility_files(self) -> None:
        catches_csv = self.root / "catches.csv"
        summary_json = self.root / "summary.json"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id,caught_at_utc,result_type,name,length_inches,quantity,
                       confidence,recognition_status,fight_milliseconds,
                       cast_to_result_milliseconds,cast_release_method,
                       automation_mode,controller_profile
                FROM catches ORDER BY caught_at_utc
                """
            ).fetchall()
            columns = [
                description[0]
                for description in connection.execute(
                    "SELECT event_id,caught_at_utc,result_type,name,length_inches,quantity,confidence,recognition_status,fight_milliseconds,cast_to_result_milliseconds,cast_release_method,automation_mode,controller_profile FROM catches LIMIT 0"
                ).description
            ]
            species = connection.execute(
                """
                SELECT name, SUM(quantity), MIN(length_inches), MAX(length_inches),
                       SUM(length_inches * quantity) * 1.0 / NULLIF(SUM(CASE WHEN length_inches IS NOT NULL THEN quantity ELSE 0 END), 0)
                FROM catches WHERE result_type='fish' AND name IS NOT NULL GROUP BY name ORDER BY name
                """
            ).fetchall()
        temporary_csv = catches_csv.with_suffix(".csv.tmp")
        with temporary_csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)
        temporary_csv.replace(catches_csv)
        snapshot = self.load_snapshot()
        summary = {
            "schema_version": SCHEMA_VERSION,
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "performance": {
                "fish_caught": snapshot.lifetime_fish,
                "items_caught": snapshot.lifetime_items,
                "escapes": snapshot.lifetime_escapes,
            },
            "species": {
                name: {
                    "quantity": quantity,
                    "shortest_length_inches": shortest,
                    "longest_length_inches": longest,
                    "average_length_inches": average,
                }
                for name, quantity, shortest, longest, average in species
            },
        }
        temporary_json = summary_json.with_suffix(".json.tmp")
        temporary_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        temporary_json.replace(summary_json)

    def _enqueue(self, operation: str, *payload: Any) -> None:
        if not self._closed:
            self._commands.put_nowait((operation, payload))

    def _writer(self) -> None:
        try:
            with self._connect() as connection:
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
                    try:
                        connection.execute("BEGIN")
                        for operation, payload in batch:
                            self._execute(connection, operation, payload)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    finally:
                        for _ in batch:
                            self._commands.task_done()
        except Exception as exc:
            self._error = str(exc)

    def _execute(
        self,
        connection: sqlite3.Connection,
        operation: str,
        payload: tuple[Any, ...],
    ) -> None:
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
        elif operation == "catch":
            record, card = payload
            card_path = None
            if card is not None:
                path = self.unknown_directory / f"{record.event_id}.png"
                cv2.imwrite(str(path), card)
                card_path = str(path)
            connection.execute(
                """
                INSERT OR IGNORE INTO catches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.event_id,
                    record.encounter_id,
                    record.caught_at_utc,
                    record.result_type.value,
                    record.name,
                    record.length_inches,
                    max(1, record.quantity),
                    record.confidence,
                    record.recognition_status.value,
                    record.fight_milliseconds,
                    record.cast_to_result_milliseconds,
                    record.cast_release_method.value if record.cast_release_method else None,
                    record.automation_mode.value,
                    record.controller_profile.value,
                    card_path,
                    SCHEMA_VERSION,
                ),
            )
        elif operation == "session-close":
            session_id, ended_at, reason, runtime = payload
            connection.execute(
                "UPDATE sessions SET ended_at_utc=?,stop_reason=?,runtime_milliseconds=? WHERE session_id=?",
                (ended_at, reason, runtime, session_id),
            )

    def _initialize_database(self) -> None:
        with self._connect() as connection:
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
                    stop_reason TEXT
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
                CREATE INDEX IF NOT EXISTS idx_encounters_session ON encounters(session_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_catches_time ON catches(caught_at_utc DESC);
                CREATE INDEX IF NOT EXISTS idx_catches_species ON catches(result_type, name);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES(?,?)",
                (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )
            connection.execute(
                "UPDATE sessions SET stop_reason='unclean-shutdown', ended_at_utc=? WHERE ended_at_utc IS NULL",
                (datetime.now(UTC).isoformat(),),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=2.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        return connection
