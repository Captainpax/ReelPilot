from __future__ import annotations

import sys
from collections import deque
from threading import Lock

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..domain import RuntimeSnapshot


class SnapshotStore:
    def __init__(self, initial: RuntimeSnapshot) -> None:
        self._snapshot = initial
        self._events: deque[tuple[str, str]] = deque(maxlen=10)
        self._lock = Lock()

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def log(self, message: str, level: str) -> None:
        with self._lock:
            if not self._events or self._events[-1] != (level, message):
                self._events.append((level, message))

    def read(self) -> tuple[RuntimeSnapshot, tuple[tuple[str, str], ...]]:
        with self._lock:
            return self._snapshot, tuple(self._events)


class RichDashboard:
    def __init__(self, initial: RuntimeSnapshot, console: Console | None = None) -> None:
        self.console = console or Console()
        self.store = SnapshotStore(initial)
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=5,
            screen=True,
            redirect_stdout=True,
            redirect_stderr=True,
        )
        self._closed = False
        self._live.start()

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self.store.publish(snapshot)
        self._live.update(self._render(), refresh=False)

    def log(self, message: str, *, level: str = "info") -> None:
        self.store.log(message, level)
        self._live.update(self._render(), refresh=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._live.stop()
        snapshot, events = self.store.read()
        self.console.print(render_dashboard(snapshot, events, self.console.width))

    def _render(self) -> RenderableType:
        snapshot, events = self.store.read()
        return render_dashboard(snapshot, events, self.console.width)


class PlainDashboard:
    def __init__(self, initial: RuntimeSnapshot, console: Console | None = None) -> None:
        self.console = console or Console(file=sys.stdout, force_terminal=False)
        self._last_state = initial.state
        self._last_message = ""
        self._snapshot = initial
        self._closed = False

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self._snapshot = snapshot
        if snapshot.state != self._last_state or snapshot.message != self._last_message:
            self.console.print(f"[{snapshot.state.value}] {snapshot.message}")
            self._last_state = snapshot.state
            self._last_message = snapshot.message

    def log(self, message: str, *, level: str = "info") -> None:
        self.console.print(f"{level.upper():7} {message}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        snapshot = self._snapshot
        self.console.print(
            f"Session complete: {snapshot.session_fish} fish, "
            f"{snapshot.session_items} items, {snapshot.session_escapes} escapes."
        )


def render_dashboard(
    snapshot: RuntimeSnapshot,
    events: tuple[tuple[str, str], ...] = (),
    width: int = 120,
) -> RenderableType:
    title = Text(" REELPILOT ", style="bold black on bright_cyan")
    status = "PAUSED" if snapshot.paused else ("CONNECTED" if snapshot.connected else "WAITING")
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right")
    header.add_row(title, f"[bold]{status}[/]  F7 Pause/Resume  F8 Stop")

    current = Table.grid(padding=(0, 1), expand=True)
    current.add_column(style="cyan", no_wrap=True)
    current.add_column(ratio=1)
    current.add_row("Task", snapshot.message)
    current.add_row("State", f"{snapshot.state.value}  {snapshot.state_elapsed_seconds:0.1f}s")
    current.add_row(
        "Mode", f"{snapshot.automation_mode.value} / {snapshot.controller_profile.value}"
    )
    current.add_row("Cast", _bar(snapshot.cast_charge_ratio, 22))
    current.add_row("Catch", _bar(snapshot.catch_progress_ratio, 22))
    current.add_row(
        "Control",
        f"duty {snapshot.duty_ratio:0.2f}  confidence {snapshot.detector_confidence:0.2f}",
    )
    if snapshot.bite_seconds_remaining is not None:
        current.add_row("Bite", f"{snapshot.bite_seconds_remaining:0.1f}s remaining")

    stats = Table.grid(padding=(0, 1), expand=True)
    stats.add_column(style="green")
    stats.add_column(justify="right")
    stats.add_row("Session fish", str(snapshot.session_fish))
    stats.add_row("Other items", str(snapshot.session_items))
    stats.add_row("Escapes", str(snapshot.session_escapes))
    stats.add_row("Lifetime fish", str(snapshot.lifetime_fish))

    catches = Table.grid(expand=True)
    catches.add_column()
    if snapshot.recent_catches:
        for label in snapshot.recent_catches[:6]:
            catches.add_row(f"• {label}")
    else:
        catches.add_row("[dim]No catches recorded yet[/]")

    log_table = Table.grid(expand=True)
    log_table.add_column(width=7)
    log_table.add_column(ratio=1)
    colors = {"error": "red", "warning": "yellow", "info": "blue", "debug": "dim"}
    for level, message in events[-7:]:
        log_table.add_row(f"[{colors.get(level, 'white')}]{level.upper()}[/]", message)
    if not events:
        log_table.add_row("[dim]INFO[/]", "ReelPilot is ready.")

    if width >= 100:
        body = Table.grid(expand=True)
        body.add_column(ratio=2)
        body.add_column(ratio=1)
        body.add_row(
            Panel(current, title="Current task", border_style="cyan"),
            Panel(stats, title="Statistics", border_style="green"),
        )
        body.add_row(
            Panel(log_table, title="Recent events", border_style="blue"),
            Panel(catches, title="Recent catches", border_style="magenta"),
        )
        content: RenderableType = body
    else:
        content = Group(
            Panel(current, title="Current task", border_style="cyan"),
            Panel(stats, title="Statistics", border_style="green"),
            Panel(catches, title="Recent catches", border_style="magenta"),
            Panel(log_table, title="Recent events", border_style="blue"),
        )
    return Group(Panel(header, border_style="bright_cyan"), content)


def _bar(value: float, width: int) -> str:
    ratio = max(0.0, min(1.0, value))
    filled = round(width * ratio)
    return f"[bright_green]{'━' * filled}[/][grey35]{'─' * (width - filled)}[/] {ratio:3.0%}"
