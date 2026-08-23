from io import StringIO

from rich.console import Console

from reelpilot.domain import (
    AutomationMode,
    AutomationState,
    ControllerProfile,
    DashboardView,
    DifficultyTier,
    RuntimeSnapshot,
    StartMode,
)
from reelpilot.stats import HistoricalStatsSnapshot, SessionStats, SpeciesStats
from reelpilot.ui import PlainDashboard, render_dashboard


def test_dashboard_renders_current_task_and_statistics() -> None:
    snapshot = RuntimeSnapshot(
        AutomationState.FISHING,
        4.2,
        True,
        False,
        AutomationMode.CONTINUOUS,
        ControllerProfile.AUTO,
        catch_progress_ratio=0.72,
        session_fish=3,
        lifetime_fish=17,
        recent_catches=("Bullhead — 31 in.",),
        message="Fishing control active",
    )
    console = Console(record=True, width=120, color_system=None)
    console.print(render_dashboard(snapshot, width=120))
    output = console.export_text()
    assert "REELPILOT" in output
    assert "Fishing control active" in output
    assert "Bullhead" in output


def test_ready_dashboard_shows_start_hotkeys() -> None:
    snapshot = RuntimeSnapshot(
        AutomationState.READY,
        1.0,
        True,
        False,
        AutomationMode.CONTINUOUS,
        ControllerProfile.AUTO,
        message="Ready",
    )
    console = Console(record=True, width=120, color_system=None)
    console.print(render_dashboard(snapshot, width=120))

    output = console.export_text()
    assert "F6 Debug Start" in output
    assert "F7 Start" in output


def test_plain_shutdown_summary_reports_treasure_transfer() -> None:
    stream = StringIO()
    console = Console(file=stream, color_system=None)
    initial = RuntimeSnapshot(
        AutomationState.READY,
        0.0,
        True,
        False,
        AutomationMode.CONTINUOUS,
        ControllerProfile.AUTO,
    )
    dashboard = PlainDashboard(initial, console)
    dashboard.publish(
        RuntimeSnapshot(
            AutomationState.STOPPED,
            1.0,
            True,
            False,
            AutomationMode.CONTINUOUS,
            ControllerProfile.AUTO,
            session_fish=1,
            session_treasure_collected=1,
            session_treasure_looted=1,
        )
    )
    dashboard.close()

    output = " ".join(stream.getvalue().split())
    assert "1 treasure secured" in output
    assert "1 chest menus looted" in output


def test_debug_dashboard_shows_recording_usage() -> None:
    snapshot = RuntimeSnapshot(
        AutomationState.FISHING,
        1.0,
        True,
        False,
        AutomationMode.CONTINUOUS,
        ControllerProfile.AUTO,
        message="Fishing",
        start_mode=StartMode.DEBUG,
        recording_path="recordings/demo",
        recorded_image_bytes=1024,
        recording_image_limit_bytes=2048,
    )
    console = Console(record=True, width=120, color_system=None)
    console.print(render_dashboard(snapshot, width=120))

    output = console.export_text()
    assert "debug" in output
    assert "1.0 KB / 2.0 KB" in output


def test_history_dashboard_renders_species_rarity_and_value() -> None:
    history = HistoricalStatsSnapshot(
        sessions=8,
        runtime_milliseconds=3_600_000,
        casts=20,
        bites=13,
        fish=4,
        fish_encounters=4,
        items=3,
        unknown_results=2,
        escapes=7,
        aborted=7,
        estimated_minimum_value_gold=300,
        estimated_maximum_value_gold=900,
        species=(
            SpeciesStats(
                "Bullhead",
                4,
                4,
                1.0,
                DifficultyTier.MEDIUM,
                20,
                25.5,
                31,
                75,
                300,
                900,
                "2026-08-22T00:00:00+00:00",
            ),
        ),
        recent_sessions=(
            SessionStats(
                "session-1",
                "2026-08-22T00:00:00+00:00",
                60_000,
                4,
                1,
                0,
                "f8",
            ),
        ),
    )
    snapshot = RuntimeSnapshot(
        AutomationState.PAUSED,
        1.0,
        True,
        True,
        AutomationMode.CONTINUOUS,
        ControllerProfile.AUTO,
        dashboard_view=DashboardView.HISTORY,
        historical_stats=history,
    )
    console = Console(record=True, width=120, color_system=None)
    console.print(render_dashboard(snapshot, width=120))

    output = console.export_text()
    assert "REELPILOT HISTORY" in output
    assert "Bullhead" in output
    assert "100.0%" in output
    assert "300–900g" in output
    assert "F5 Back" in output
