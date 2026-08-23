"""Deterministically replay recorded observations without capture or input."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from ..control import FishingController
from ..domain import ControllerProfile, ControlPhase, FishingObservation


class ReplayRunner:
    """Offline deterministic replay; never imports live input or capture components."""

    def run(
        self,
        session_path: Path,
        profile: ControllerProfile = ControllerProfile.AUTO,
    ) -> dict[str, object]:
        """Replay ``session_path`` and write a metrics-rich ``report.json``."""
        telemetry_path = session_path / "telemetry.jsonl"
        if not telemetry_path.is_file():
            raise RuntimeError(f"telemetry not found: {telemetry_path}")
        manifest_path = session_path / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.is_file()
            else {}
        )
        source_format_version = int(manifest.get("format_version", 1))
        controller = FishingController(profile)
        duties: list[float] = []
        errors: list[float] = []
        cycle_values: list[float] = []
        dropouts = 0
        direction_changes = 0
        previous_direction = 0
        saturation = 0
        event_count = 0
        telemetry_event_count = 0
        roi_capture_count = 0
        full_capture_count = 0
        food_consumed = 0
        inventory_full_stops = 0
        minimum_energy_ratio: float | None = None
        stage_timings: dict[str, list[float]] = defaultdict(list)
        fallback_stage_timings: dict[str, list[float]] = defaultdict(list)
        encounter_data: dict[str, dict[str, Any]] = {}
        for line in telemetry_path.open(encoding="utf-8"):
            event = json.loads(line)
            telemetry_event_count += 1
            encounter_id = str(event.get("encounter_id") or "unassigned")
            encounter = encounter_data.setdefault(
                encounter_id,
                {
                    "observations": 0,
                    "dropouts": 0,
                    "controls": 0,
                    "centered": 0,
                    "contained": 0,
                    "edge_risk": 0,
                    "saturated": 0,
                    "profiles": Counter(),
                    "result_scans": 0,
                    "progress_peak": 0.0,
                    "outcome": None,
                    "cast_release_method": None,
                    "max_verification": None,
                    "perfect_status": None,
                    "minimum_predicted_margin_pixels": None,
                    "safety_overrides": 0,
                    "control_phases": Counter(),
                    "control_targets": Counter(),
                    "treasure_seen": False,
                    "treasure_attempts": 0,
                    "treasure_collected": False,
                    "treasure_looted": False,
                },
            )
            event_name = event.get("event")
            if event_name == "food-consumed":
                food_consumed += 1
            elif event_name == "inventory-stop":
                inventory_full_stops += 1
            elif event_name == "energy-observation" and isinstance(
                event.get("fill_ratio"), (int, float)
            ):
                energy_ratio = float(event["fill_ratio"])
                minimum_energy_ratio = (
                    energy_ratio
                    if minimum_energy_ratio is None
                    else min(minimum_energy_ratio, energy_ratio)
                )
            timings = event.get("timings_ms")
            if isinstance(timings, dict):
                for stage, value in timings.items():
                    if isinstance(value, (int, float)):
                        destination = (
                            stage_timings
                            if event_name == "cycle-timing"
                            else fallback_stage_timings
                        )
                        destination[str(stage)].append(float(value))
            if (
                event_name == "cycle-timing"
                and isinstance(timings, dict)
                and isinstance(timings.get("total"), (int, float))
            ):
                cycle_values.append(float(timings["total"]))
            if event_name == "control-decision":
                encounter["controls"] += 1
                duty_value = float(event.get("duty_ratio", 0.5))
                if duty_value <= 0.001 or duty_value >= 0.999:
                    encounter["saturated"] += 1
                bar_length = float(event.get("bar_length_pixels") or 96.0)
                feasible_error = abs(float(event.get("feasible_error_pixels", 0.0)))
                if feasible_error / max(1.0, bar_length) <= 0.10:
                    encounter["centered"] += 1
                if feasible_error / max(1.0, bar_length) <= 0.50:
                    encounter["contained"] += 1
                edge_clearance = event.get("edge_clearance_ratio")
                if isinstance(edge_clearance, (int, float)) and edge_clearance <= 0.15:
                    encounter["edge_risk"] += 1
                encounter["profiles"][str(event.get("active_profile", "unknown"))] += 1
                encounter["control_phases"][
                    str(event.get("control_phase", "recovery"))
                ] += 1
                encounter["control_targets"][
                    str(event.get("control_target", "fish"))
                ] += 1
                predicted_margin = event.get("predicted_margin_pixels")
                if isinstance(predicted_margin, (int, float)):
                    current_minimum = encounter["minimum_predicted_margin_pixels"]
                    encounter["minimum_predicted_margin_pixels"] = (
                        float(predicted_margin)
                        if current_minimum is None
                        else min(float(current_minimum), float(predicted_margin))
                    )
                if event.get("safety_override") is True:
                    encounter["safety_overrides"] += 1
            elif event_name == "result-scan":
                encounter["result_scans"] += 1
            elif event_name == "encounter-terminal":
                encounter["outcome"] = event.get("outcome")
            elif event_name == "cast-release":
                encounter["cast_release_method"] = event.get("release_method")
                encounter["max_verification"] = event.get("max_verification")
            elif event_name == "perfect-status":
                encounter["perfect_status"] = event.get("perfect_status")
            elif event_name == "treasure-status":
                status = str(event.get("treasure_status", "none"))
                encounter["treasure_seen"] |= status != "none"
                encounter["treasure_attempts"] = max(
                    int(encounter["treasure_attempts"]),
                    int(event.get("treasure_attempts", 0)),
                )
                encounter["treasure_collected"] |= status in {"collected", "looted"}
                encounter["treasure_looted"] |= status == "looted"
            elif event_name == "treasure-loot" and event.get("looted") is True:
                encounter["treasure_looted"] = True
            observation = self._observation(event)
            if observation is None:
                continue
            event_count += 1
            if observation.used_roi_capture:
                roi_capture_count += 1
            else:
                full_capture_count += 1
            encounter["observations"] += 1
            encounter["progress_peak"] = max(
                float(encounter["progress_peak"]), observation.progress_ratio
            )
            if not observation.control_ready:
                dropouts += 1
                encounter["dropouts"] += 1
                continue
            bar_length = observation.bar_length_pixels or 96
            timestamp = float(event.get("timestamp_seconds", event.get("timestamp", 0.0)))
            duty = controller.step(
                observation,
                bar_length,
                timestamp,
                ControlPhase.PERFECT
                if source_format_version >= 5
                else ControlPhase.RECOVERY,
            )
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
            if (
                event_name != "cycle-timing"
                and isinstance(timings, dict)
                and isinstance(timings.get("total"), (int, float))
            ):
                cycle_values.append(float(timings["total"]))
        encounter_reports = []
        for encounter_id, encounter_values in encounter_data.items():
            controls = int(encounter_values["controls"])
            observations = int(encounter_values["observations"])
            if encounter_id == "unassigned" and not controls and not observations:
                continue
            profiles = encounter_values["profiles"]
            encounter_reports.append(
                {
                    "encounter_id": encounter_id,
                    "observations": observations,
                    "detection_dropouts": int(encounter_values["dropouts"]),
                    "control_events": controls,
                    "centered_fraction": encounter_values["centered"] / controls
                    if controls
                    else 0.0,
                    "contained_fraction": encounter_values["contained"] / controls
                    if controls
                    else 0.0,
                    "edge_risk_fraction": encounter_values["edge_risk"] / controls
                    if controls
                    else 0.0,
                    "saturation_fraction": encounter_values["saturated"] / controls
                    if controls
                    else 0.0,
                    "profile_occupancy": {
                        name: count / controls for name, count in profiles.items()
                    }
                    if controls
                    else {},
                    "progress_peak_ratio": encounter_values["progress_peak"],
                    "result_scans": encounter_values["result_scans"],
                    "outcome": encounter_values["outcome"],
                    "cast_release_method": encounter_values["cast_release_method"],
                    "max_verification": encounter_values["max_verification"],
                    "perfect_status": encounter_values["perfect_status"],
                    "minimum_predicted_margin_pixels": encounter_values[
                        "minimum_predicted_margin_pixels"
                    ],
                    "safety_overrides": encounter_values["safety_overrides"],
                    "control_phase_occupancy": {
                        name: count / controls
                        for name, count in encounter_values["control_phases"].items()
                    }
                    if controls
                    else {},
                    "control_target_occupancy": {
                        name: count / controls
                        for name, count in encounter_values["control_targets"].items()
                    }
                    if controls
                    else {},
                    "treasure_seen": encounter_values["treasure_seen"],
                    "treasure_attempts": encounter_values["treasure_attempts"],
                    "treasure_collected": encounter_values["treasure_collected"],
                    "treasure_looted": encounter_values["treasure_looted"],
                }
            )
        for stage, fallback_values in fallback_stage_timings.items():
            if stage not in stage_timings:
                stage_timings[stage] = fallback_values
        timing_report = {
            stage: {
                "count": len(timing_values),
                "p50": _percentile(timing_values, 0.50),
                "p95": _percentile(timing_values, 0.95),
            }
            for stage, timing_values in sorted(stage_timings.items())
        }
        report: dict[str, object] = {
            "format_version": 2,
            "telemetry_events": telemetry_event_count,
            "source_events": event_count,
            "control_events": len(duties),
            "detection_dropouts": dropouts,
            "roi_capture_count": roi_capture_count,
            "full_capture_count": full_capture_count,
            "average_duty_ratio": sum(duties) / len(duties) if duties else 0.0,
            "saturation_fraction": saturation / len(duties) if duties else 0.0,
            "direction_changes": direction_changes,
            "feasible_centered_fraction": (
                sum(abs(error) <= 0.10 for error in errors) / len(errors) if errors else 0.0
            ),
            "median_cycle_milliseconds": median(cycle_values) if cycle_values else None,
            "effective_loop_hz": (
                1000.0 / median(cycle_values)
                if cycle_values and median(cycle_values) > 0.0
                else None
            ),
            "timings_ms": timing_report,
            "encounters": encounter_reports,
            "refueling": {
                "food_consumed": food_consumed,
                "minimum_energy_ratio": minimum_energy_ratio,
                "inventory_full_stops": inventory_full_stops,
            },
        }
        if manifest_path.is_file():
            report["recording"] = {
                "format_version": manifest.get("format_version"),
                "complete": manifest.get("complete"),
                "recorded_image_bytes": manifest.get("recorded_image_bytes", 0),
                "image_limit_bytes": manifest.get("image_limit_bytes"),
                "image_limit_reached": manifest.get("image_limit_reached", False),
                "dropped_images": manifest.get("dropped_images", 0),
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
            float(
                raw.get(
                    "progress_confidence",
                    1.0 if raw.get("progress_ratio", raw.get("progress")) is not None else 0.0,
                )
            ),
            used_roi_capture=bool(raw.get("used_roi_capture", False)),
            fish_top_y_pixels=(
                float(raw["fish_top_y_pixels"])
                if raw.get("fish_top_y_pixels") is not None
                else None
            ),
            fish_bottom_y_pixels=(
                float(raw["fish_bottom_y_pixels"])
                if raw.get("fish_bottom_y_pixels") is not None
                else None
            ),
            bar_top_y_pixels=(
                float(raw["bar_top_y_pixels"])
                if raw.get("bar_top_y_pixels") is not None
                else None
            ),
            bar_bottom_y_pixels=(
                float(raw["bar_bottom_y_pixels"])
                if raw.get("bar_bottom_y_pixels") is not None
                else None
            ),
            containment_margin_pixels=(
                float(raw["containment_margin_pixels"])
                if raw.get("containment_margin_pixels") is not None
                else None
            ),
            treasure_center_y_pixels=(
                float(raw["treasure_center_y_pixels"])
                if raw.get("treasure_center_y_pixels") is not None
                else None
            ),
            treasure_top_y_pixels=(
                float(raw["treasure_top_y_pixels"])
                if raw.get("treasure_top_y_pixels") is not None
                else None
            ),
            treasure_bottom_y_pixels=(
                float(raw["treasure_bottom_y_pixels"])
                if raw.get("treasure_bottom_y_pixels") is not None
                else None
            ),
            treasure_confidence=float(raw.get("treasure_confidence", 0.0)),
        )


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * ratio)
    return ordered[index]
