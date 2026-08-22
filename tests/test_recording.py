import json
from pathlib import Path

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
