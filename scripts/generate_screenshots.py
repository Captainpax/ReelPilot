import io
from pathlib import Path

from rich.console import Console

from reelpilot.domain import (
    AutomationMode,
    AutomationState,
    ControllerProfile,
    RuntimeSnapshot,
)
from reelpilot.ui import render_dashboard

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def save(name: str, snapshot: RuntimeSnapshot) -> None:
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
    base = dict(
        connected=True,
        paused=False,
        automation_mode=AutomationMode.CONTINUOUS,
        controller_profile=ControllerProfile.AUTO,
        session_fish=3,
        session_items=1,
        session_escapes=0,
        lifetime_fish=42,
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


if __name__ == "__main__":
    main()
