"""Generate terminal-only README screenshots from sanitized immutable snapshots."""

import io
from pathlib import Path

from rich.console import Console

from reelpilot.domain import (
    AutomationMode,
    AutomationState,
    ControllerProfile,
    DashboardView,
    DifficultyTier,
    EnergyStatus,
    RuntimeSnapshot,
    StartMode,
)
from reelpilot.stats import HistoricalStatsSnapshot, SessionStats, SpeciesStats
from reelpilot.ui import render_dashboard

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def save(name: str, snapshot: RuntimeSnapshot) -> None:
    """Render ``snapshot`` and save it as a deterministic Rich SVG."""
    console = Console(
        file=io.StringIO(),
        record=True,
        width=120,
        height=32,
        color_system="truecolor",
        force_terminal=True,
    )
    console.print(render_dashboard(snapshot, (("info", "Sanitized demonstration data"),), 120))
    console.save_svg(str(OUTPUT / name), title="ReelPilot")


def main() -> None:
    """Regenerate active, paused, history, and final demonstration dashboards."""
    base = dict(
        connected=True,
        paused=False,
        automation_mode=AutomationMode.CONTINUOUS,
        controller_profile=ControllerProfile.AUTO,
        session_fish=3,
        session_items=1,
        session_escapes=0,
        lifetime_fish=42,
        energy_ratio=0.68,
        energy_status=EnergyStatus.OK,
        session_food_consumed=1,
        lifetime_food_consumed=9,
        recent_catches=("Bullhead — 31 in.", "Bream — 18 in.", "Seaweed"),
    )
    save(
        "dashboard-active.svg",
        RuntimeSnapshot(
            AutomationState.FISHING,
            6.4,
            **base,
            cast_charge_ratio=1.0,
            catch_progress_ratio=0.73,
            duty_ratio=0.81,
            detector_confidence=0.92,
            start_mode=StartMode.DEBUG,
            recording_path=r"C:\Users\Player\AppData\Local\ReelPilot\recordings\demo",
            recorded_image_bytes=84 * 1024 * 1024,
            recording_image_limit_bytes=2 * 1024 * 1024 * 1024,
            message="Fishing control active",
        ),
    )
    save(
        "dashboard-paused.svg",
        RuntimeSnapshot(
            AutomationState.PAUSED,
            2.1,
            **(base | {"paused": True}),
            catch_progress_ratio=0.51,
            message="Paused; input released",
        ),
    )
    save(
        "dashboard-summary.svg",
        RuntimeSnapshot(
            AutomationState.STOPPED,
            0.0,
            **(base | {"connected": False}),
            message="Stopped safely — 3 fish, 1 item, 0 escapes",
        ),
    )
    history = HistoricalStatsSnapshot(
        sessions=12,
        runtime_milliseconds=4_860_000,
        casts=64,
        bites=57,
        fish=42,
        fish_encounters=40,
        items=8,
        unknown_results=2,
        escapes=6,
        timeouts=1,
        aborted=1,
        food_consumed=9,
        inventory_full_stops=1,
        minimum_energy_ratio=0.29,
        estimated_minimum_value_gold=4_125,
        estimated_maximum_value_gold=12_375,
        species=(
            SpeciesStats(
                "Bullhead", 14, 14, 1 / 3, DifficultyTier.MEDIUM,
                18, 25.4, 31, 75, 1_050, 3_150, "2026-08-22T06:42:00+00:00",
            ),
            SpeciesStats(
                "Bream", 10, 10, 10 / 42, DifficultyTier.MEDIUM,
                12, 16.8, 20, 45, 450, 1_350, "2026-08-22T06:35:00+00:00",
            ),
            SpeciesStats(
                "Anchovy", 8, 8, 8 / 42, DifficultyTier.EASY,
                5, 9.1, 13, 30, 240, 720, "2026-08-22T06:28:00+00:00",
            ),
        ),
        recent_sessions=(
            SessionStats(
                "demo-1", "2026-08-22T06:00:00+00:00", 900_000, 8, 1, 1, "f8"
            ),
        ),
        recent_catches=(
            "Bullhead — 31 in. — 75–225g est.",
            "Bream — 18 in. — 45–135g est.",
        ),
    )
    save(
        "dashboard-history.svg",
        RuntimeSnapshot(
            AutomationState.PAUSED,
            2.1,
            **(base | {"paused": True}),
            message="Paused; input released",
            dashboard_view=DashboardView.HISTORY,
            historical_stats=history,
        ),
    )


if __name__ == "__main__":
    main()
