import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from reelpilot.domain import (
    AutomationMode,
    CastReleaseMethod,
    ControllerProfile,
    RecognitionStatus,
    ResultType,
)
from reelpilot.stats import CatchRecord, SQLiteStatsRepository, StatsService


def make_record(encounter_id: str, name: str = "Bullhead") -> CatchRecord:
    return CatchRecord(
        "event-1",
        encounter_id,
        datetime.now(UTC).isoformat(),
        ResultType.FISH,
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
