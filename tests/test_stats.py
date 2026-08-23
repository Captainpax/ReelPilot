import csv
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reelpilot.domain import (
    AutomationMode,
    CastReleaseMethod,
    ControllerProfile,
    MaxVerification,
    PerfectStatus,
    RecognitionStatus,
    ResultType,
)
from reelpilot.stats import (
    CatchRecord,
    SQLiteStatsRepository,
    StatsService,
    find_catalog_entry,
)


def make_record(
    encounter_id: str,
    name: str = "Bullhead",
    *,
    event_id: str = "event-1",
    result_type: ResultType = ResultType.FISH,
) -> CatchRecord:
    return CatchRecord(
        event_id,
        encounter_id,
        datetime.now(UTC).isoformat(),
        result_type,
        name,
        31,
        1,
        0.99,
        RecognitionStatus.RECOGNIZED,
        7000,
        12000,
        CastReleaseMethod.VISUAL_MAX,
        AutomationMode.CONTINUOUS,
        ControllerProfile.AUTO,
    )


def test_sqlite_repository_records_and_exports(tmp_path: Path) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    service = StatsService(repository, "session-1", {"mode": "continuous"})
    service.begin_encounter("encounter-1", 1)
    service.record_catch(make_record("encounter-1"))
    repository.close_session("session-1", "stopped", 15000)
    repository.close()
    with sqlite3.connect(tmp_path / "reelpilot.db") as connection:
        assert connection.execute("SELECT name FROM catches").fetchone()[0] == "Bullhead"
    assert (tmp_path / "catches.csv").is_file()
    assert (tmp_path / "summary.json").is_file()
    with (tmp_path / "catches.csv").open(newline="", encoding="utf-8") as stream:
        header = next(csv.reader(stream))
    assert header[-5:] == [
        "treasure_status",
        "treasure_seen",
        "treasure_attempts",
        "treasure_collected",
        "treasure_looted",
    ]


def test_duplicate_catch_id_does_not_corrupt_database(tmp_path: Path) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    service = StatsService(repository, "session-1", {})
    service.begin_encounter("encounter-1", 1)
    service.record_catch(make_record("encounter-1"))
    service.record_catch(make_record("encounter-1"))
    repository.close()
    with sqlite3.connect(tmp_path / "reelpilot.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM catches").fetchone()[0] == 1
    assert repository.error is None


def test_catalog_estimates_base_and_possible_maximum() -> None:
    bullhead = find_catalog_entry("bullhead")
    seaweed = find_catalog_entry("Seaweed")

    assert bullhead is not None
    assert bullhead.estimate_value(2) == (150, 450)
    assert bullhead.difficulty_tier is not None
    assert bullhead.difficulty_tier.value == "medium"
    assert seaweed is not None
    assert seaweed.estimate_value(2) == (40, 40)


def test_refresh_flushes_pending_writes_and_builds_history(tmp_path: Path) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    service = StatsService(repository, "session-1", {})
    service.begin_encounter("encounter-1", 1)
    service.record_catch(make_record("encounter-1"))
    service.begin_encounter("encounter-2", 2)
    service.record_catch(
        make_record("encounter-2", "Anchovy", event_id="event-2")
    )
    service.begin_encounter("encounter-3", 3)
    service.record_catch(
        replace(
            make_record(
                "encounter-3",
                "Seaweed",
                event_id="event-3",
                result_type=ResultType.ITEM,
            ),
            length_inches=None,
        )
    )

    history = service.refresh_history()

    assert history.casts == 3
    assert history.fish == 2
    assert history.items == 1
    assert history.estimated_minimum_value_gold == 125
    assert history.estimated_maximum_value_gold == 335
    assert {row.name: row.observed_share_ratio for row in history.species} == {
        "Anchovy": 0.5,
        "Bullhead": 0.5,
    }
    repository.close()


def test_concurrent_core_catches_are_not_dropped(tmp_path: Path) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    service = StatsService(repository, "session-1", {})

    def record(sequence: int) -> None:
        encounter_id = f"encounter-{sequence}"
        service.begin_encounter(encounter_id, sequence)
        service.record_catch(
            make_record(encounter_id, event_id=f"event-{sequence}")
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(record, range(1, 21)))

    assert repository.flush()
    with sqlite3.connect(tmp_path / "reelpilot.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM catches").fetchone()[0] == 20
    repository.close()


def test_schema_v1_migration_backfills_and_reconciles(tmp_path: Path) -> None:
    database = tmp_path / "reelpilot.db"
    tmp_path.mkdir(exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at_utc TEXT NOT NULL);
            CREATE TABLE sessions(session_id TEXT PRIMARY KEY,started_at_utc TEXT NOT NULL,ended_at_utc TEXT,runtime_milliseconds INTEGER,settings_json TEXT NOT NULL,stop_reason TEXT);
            CREATE TABLE encounters(encounter_id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(session_id),sequence INTEGER NOT NULL,started_at_utc TEXT NOT NULL,cast_started_at_utc TEXT,cast_release_method TEXT,bite_at_utc TEXT,minigame_started_at_utc TEXT,ended_at_utc TEXT,outcome TEXT NOT NULL DEFAULT 'active',fight_milliseconds INTEGER,cast_to_result_milliseconds INTEGER,controller_profile TEXT);
            CREATE TABLE catches(event_id TEXT PRIMARY KEY,encounter_id TEXT NOT NULL UNIQUE REFERENCES encounters(encounter_id),caught_at_utc TEXT NOT NULL,result_type TEXT NOT NULL,name TEXT,length_inches INTEGER,quantity INTEGER NOT NULL,confidence REAL NOT NULL,recognition_status TEXT NOT NULL,fight_milliseconds INTEGER,cast_to_result_milliseconds INTEGER,cast_release_method TEXT,automation_mode TEXT NOT NULL,controller_profile TEXT NOT NULL,card_path TEXT,schema_version INTEGER NOT NULL);
            INSERT INTO schema_migrations VALUES(1,'2026-01-01T00:00:00+00:00');
            INSERT INTO sessions VALUES('session-1','2026-01-01T00:00:00+00:00','2026-01-01T00:01:00+00:00',60000,'{}','f8');
            INSERT INTO encounters VALUES('encounter-1','session-1',1,'2026-01-01T00:00:00+00:00',NULL,NULL,NULL,NULL,NULL,'active',NULL,NULL,NULL);
            INSERT INTO catches VALUES('event-1','encounter-1','2026-01-01T00:00:30+00:00','fish','Bullhead',31,1,0.99,'recognized',7000,12000,'visual-max','continuous','auto',NULL,1);
            """
        )

    repository = SQLiteStatsRepository(tmp_path)
    history = repository.load_history()

    assert history.aborted == 1
    assert history.fish == 1
    assert history.estimated_minimum_value_gold == 75
    assert history.estimated_maximum_value_gold == 225
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT qualified_item_id,fish_difficulty_score FROM catches"
        ).fetchone()
        assert row == ("(O)700", 46)
        versions = {
            value[0] for value in connection.execute("SELECT version FROM schema_migrations")
        }
        assert versions == {1, 2, 3, 4, 5}
        encounter_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(encounters)")
        }
        assert {
            "perfect_status",
            "containment_breaks",
            "minimum_margin_pixels",
            "max_verification",
            "treasure_status",
            "treasure_seen",
            "treasure_attempts",
            "treasure_collected",
            "treasure_looted",
        } <= encounter_columns
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)")
        }
        assert {
            "food_consumed",
            "minimum_energy_ratio",
            "inventory_full_stops",
        } <= session_columns
    repository.close()


def test_writer_failure_releases_flush_barrier(tmp_path: Path) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    repository._enqueue("unsupported-test-operation")

    assert not repository.flush(timeout_seconds=1.0)
    assert repository.error is not None
    repository.close()


def test_refresh_timeout_preserves_a_bounded_failure(tmp_path: Path, monkeypatch) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    monkeypatch.setattr(repository, "flush", lambda timeout_seconds=2.0: False)

    with pytest.raises(TimeoutError, match="did not complete"):
        repository.refresh_history(timeout_seconds=0.01)

    repository.close()


def test_repository_close_releases_database_file(tmp_path: Path) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    repository.load_snapshot()
    repository.load_history()
    repository.close()

    database = tmp_path / "reelpilot.db"
    renamed = tmp_path / "released.db"
    database.replace(renamed)
    renamed.replace(database)


def test_history_aggregates_perfect_and_verified_max_statuses(tmp_path: Path) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    service = StatsService(repository, "session-1", {})
    service.begin_encounter("encounter-1", 1)
    repository.update_encounter("encounter-1", "perfect_status", "confirmed")
    repository.update_encounter("encounter-1", "max_verification", "verified")
    service.record_catch(
        replace(
            make_record("encounter-1"),
            perfect_status=PerfectStatus.CONFIRMED,
            max_verification=MaxVerification.VERIFIED,
        )
    )

    history = service.refresh_history()

    assert history.perfect_confirmed == 1
    assert history.perfect_ratio == 1.0
    assert history.max_verified == 1
    assert history.max_verified_ratio == 1.0
    repository.close()


def test_history_counts_seen_collected_and_looted_treasure(tmp_path: Path) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    service = StatsService(repository, "session-1", {})
    service.begin_encounter("encounter-1", 1)
    repository.update_encounter("encounter-1", "treasure_seen", 1)
    repository.update_encounter("encounter-1", "treasure_attempts", 2)
    repository.update_encounter("encounter-1", "treasure_collected", 1)
    repository.update_encounter("encounter-1", "treasure_looted", 1)

    history = service.refresh_history()

    assert history.treasure_seen == 1
    assert history.treasure_attempts == 2
    assert history.treasure_collection_ratio == 1.0
    assert history.treasure_loot_ratio == 1.0
    repository.close()


def test_history_tracks_food_low_energy_and_inventory_stops(tmp_path: Path) -> None:
    repository = SQLiteStatsRepository(tmp_path)
    service = StatsService(repository, "session-1", {})

    service.record_energy(0.80)
    service.record_energy(0.30)
    service.record_energy(0.60)
    service.record_food_consumed()
    service.record_food_consumed()
    service.record_inventory_full_stop()
    history = service.refresh_history()

    assert history.food_consumed == 2
    assert history.inventory_full_stops == 1
    assert history.minimum_energy_ratio == pytest.approx(0.30)
    snapshot = service.snapshot
    assert snapshot.session_food_consumed == 2
    assert snapshot.session_inventory_full_stops == 1
    assert snapshot.session_minimum_energy_ratio == pytest.approx(0.30)
    repository.close()
