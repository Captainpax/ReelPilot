from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any

from ..control import FishingController
from ..domain import ControllerProfile, FishingObservation


class ReplayRunner:
    """Offline deterministic replay; never imports live input or capture components."""

    def run(
        self,
        session_path: Path,
        profile: ControllerProfile = ControllerProfile.AUTO,
    ) -> dict[str, object]:
        telemetry_path = session_path / "telemetry.jsonl"
        if not telemetry_path.is_file():
            raise RuntimeError(f"telemetry not found: {telemetry_path}")
        controller = FishingController(profile)
        duties: list[float] = []
        errors: list[float] = []
        cycle_values: list[float] = []
        dropouts = 0
        direction_changes = 0
        previous_direction = 0
        saturation = 0
        event_count = 0
        for line in telemetry_path.open(encoding="utf-8"):
            event = json.loads(line)
            observation = self._observation(event)
            if observation is None:
                continue
            event_count += 1
            if not observation.control_ready:
                dropouts += 1
                continue
            bar_length = observation.bar_length_pixels or 96
            timestamp = float(event.get("timestamp_seconds", event.get("timestamp", 0.0)))
            duty = controller.step(observation, bar_length, timestamp)
            duties.append(duty)
            if controller.last_decision is not None:
                error = controller.last_decision.feasible_error_pixels / bar_length
                errors.append(error)
                direction = 1 if duty > 0.51 else (-1 if duty < 0.49 else 0)
                if previous_direction and direction and direction != previous_direction:
                    direction_changes += 1
                if direction:
                    previous_direction = direction
            if duty <= 0.001 or duty >= 0.999:
                saturation += 1
            timings = event.get("timings_ms")
            if isinstance(timings, dict) and isinstance(timings.get("total"), (int, float)):
                cycle_values.append(float(timings["total"]))
        report: dict[str, object] = {
            "format_version": 1,
            "source_events": event_count,
            "control_events": len(duties),
            "detection_dropouts": dropouts,
            "average_duty_ratio": sum(duties) / len(duties) if duties else 0.0,
            "saturation_fraction": saturation / len(duties) if duties else 0.0,
            "direction_changes": direction_changes,
            "feasible_centered_fraction": (
                sum(abs(error) <= 0.10 for error in errors) / len(errors) if errors else 0.0
            ),
            "median_cycle_milliseconds": median(cycle_values) if cycle_values else None,
        }
        (session_path / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        return report

    @staticmethod
    def _observation(event: dict[str, Any]) -> FishingObservation | None:
        raw = event.get("observation")
        if not isinstance(raw, dict):
            if event.get("event") not in {"control", "observation"}:
                return None
            raw = event
        # Format 1/2 adapter uses the former short field names; format 3 uses
        # explicit unit-bearing names.
        fish = raw.get("fish_center_y_pixels", raw.get("fish_y"))
        bar = raw.get("bar_center_y_pixels", raw.get("fishing_bar_y"))
        length = raw.get("bar_length_pixels", raw.get("observed_bar_length"))
        return FishingObservation(
            bool(raw.get("ui_detected", fish is not None and bar is not None)),
            float(fish) if fish is not None else None,
            float(bar) if bar is not None else None,
            int(length) if length is not None else None,
            float(raw.get("progress_ratio", raw.get("progress", 0.0))),
            float(raw.get("fish_confidence", 1.0 if fish is not None else 0.0)),
            float(raw.get("bar_confidence", 1.0 if bar is not None else 0.0)),
        )
