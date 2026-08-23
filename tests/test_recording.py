import json
from pathlib import Path

import numpy as np

from reelpilot.domain import ControllerProfile
from reelpilot.recording import ReplayRunner, SessionRecorder


def test_recorder_writes_manifest_and_telemetry(tmp_path: Path) -> None:
    recorder = SessionRecorder(tmp_path, {"mode": "test"})
    recorder.record("state", "startup", 1.0, {"message": "ready"})
    path = recorder.path
    recorder.close()
    assert json.loads((path / "manifest.json").read_text())["complete"]
    assert "ready" in (path / "telemetry.jsonl").read_text()


def test_replay_supports_legacy_observation_fields(tmp_path: Path) -> None:
    session = tmp_path / "legacy"
    session.mkdir()
    event = {
        "timestamp": 1.0,
        "event": "observation",
        "observation": {
            "ui_detected": True,
            "fish_y": 200,
            "fishing_bar_y": 190,
            "observed_bar_length": 96,
            "fish_confidence": 1.0,
            "bar_confidence": 1.0,
        },
    }
    (session / "telemetry.jsonl").write_text(json.dumps(event) + "\n")
    report = ReplayRunner().run(session, ControllerProfile.NORMAL)
    assert report["control_events"] == 1
    assert (session / "report.json").is_file()


def test_recorder_caps_optional_images_but_keeps_telemetry(tmp_path: Path) -> None:
    recorder = SessionRecorder(tmp_path, {}, image_limit_bytes=1)
    recorder.record(
        "observation",
        "fishing",
        1.0,
        {"value": 1},
        image=("crop", np.zeros((20, 20, 3), dtype=np.uint8)),
    )
    path = recorder.path
    recorder.close()

    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["format_version"] == 7
    assert manifest["image_limit_reached"]
    assert "\"value\":1" in (path / "telemetry.jsonl").read_text()


def test_dropped_images_never_leave_dangling_telemetry_paths(tmp_path: Path) -> None:
    recorder = SessionRecorder(tmp_path, {}, image_queue_size=0)
    recorder.record(
        "observation",
        "fishing",
        1.0,
        image=("crop", np.zeros((20, 20, 3), dtype=np.uint8)),
    )
    path = recorder.path
    recorder.close()

    event = json.loads((path / "telemetry.jsonl").read_text())
    assert "image" not in event
    assert recorder.dropped_images == 1


def test_replay_reports_version_four_encounters_and_timings(tmp_path: Path) -> None:
    session = tmp_path / "debug"
    session.mkdir()
    events = [
        {
            "event": "cycle-timing",
            "encounter_id": "one",
            "timings_ms": {"capture": 4.0, "total": 12.0},
        },
        {
            "event": "control-decision",
            "encounter_id": "one",
            "duty_ratio": 0.8,
            "feasible_error_pixels": 5.0,
            "bar_length_pixels": 100,
            "edge_clearance_ratio": 0.45,
            "active_profile": "normal",
        },
        {
            "event": "encounter-terminal",
            "encounter_id": "one",
            "outcome": "fish",
        },
    ]
    (session / "telemetry.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )

    report = ReplayRunner().run(session)

    assert report["format_version"] == 2
    assert report["effective_loop_hz"] == 1000.0 / 12.0
    assert report["encounters"][0]["outcome"] == "fish"  # type: ignore[index]


def test_replay_accepts_v6_and_v7_refueling_events(tmp_path: Path) -> None:
    for version in (6, 7):
        session = tmp_path / f"version-{version}"
        session.mkdir()
        (session / "manifest.json").write_text(
            json.dumps({"format_version": version, "complete": True})
        )
        events = (
            {"event": "energy-observation", "fill_ratio": 0.31},
            {"event": "food-consumed", "after_ratio": 0.80},
            {"event": "inventory-stop", "reason": "inventory-full"},
        )
        (session / "telemetry.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )

        report = ReplayRunner().run(session)

        assert report["recording"]["format_version"] == version  # type: ignore[index]
        assert report["refueling"] == {  # type: ignore[comparison-overlap]
            "food_consumed": 1,
            "minimum_energy_ratio": 0.31,
            "inventory_full_stops": 1,
        }
