from rich.console import Console

from reelpilot.domain import (
    AutomationMode,
    AutomationState,
    ControllerProfile,
    RuntimeSnapshot,
)
from reelpilot.ui import render_dashboard


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
