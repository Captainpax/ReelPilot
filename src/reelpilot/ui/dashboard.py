"""Rich and structured-log presentations for immutable runtime snapshots."""

from __future__ import annotations

import sys
from collections import deque
from threading import Lock

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..domain import AutomationState, DashboardView, RuntimeSnapshot
from ..stats import HistoricalStatsSnapshot


class SnapshotStore:
    """Synchronize immutable snapshots and deduplicated dashboard events."""

    def __init__(self, initial: RuntimeSnapshot) -> None:
        """Create a synchronized store seeded with ``initial``."""
        self._snapshot = initial
        self._events: deque[tuple[str, str]] = deque(maxlen=10)
        self._lock = Lock()

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        """Replace the current immutable snapshot."""
        with self._lock:
            self._snapshot = snapshot

    def log(self, message: str, level: str) -> None:
        """Append one event unless it exactly duplicates the newest event."""
        with self._lock:
            if not self._events or self._events[-1] != (level, message):
                self._events.append((level, message))

    def read(self) -> tuple[RuntimeSnapshot, tuple[tuple[str, str], ...]]:
        """Atomically return the current snapshot and event tuple."""
        with self._lock:
            return self._snapshot, tuple(self._events)


class RichDashboard:
    """Render ReelPilot in a five-hertz alternate-screen Rich dashboard."""

    def __init__(self, initial: RuntimeSnapshot, console: Console | None = None) -> None:
        """Start an alternate-screen dashboard seeded with ``initial``."""
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
        """Publish a snapshot for the next five-hertz refresh."""
        self.store.publish(snapshot)
        self._live.update(self._render(), refresh=False)

    def log(self, message: str, *, level: str = "info") -> None:
        """Add a deduplicated event to the dashboard log panel."""
        self.store.log(message, level)
        self._live.update(self._render(), refresh=False)

    def close(self) -> None:
        """Stop Rich Live and print the final dashboard idempotently."""
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
    """Emit state changes and summaries when interactive rendering is unavailable."""

    def __init__(self, initial: RuntimeSnapshot, console: Console | None = None) -> None:
        """Create a redirected-output dashboard seeded with ``initial``."""
        self.console = console or Console(file=sys.stdout, force_terminal=False)
        self._last_state = initial.state
        self._last_message = ""
        self._snapshot = initial
        self._closed = False
        self._last_view = initial.dashboard_view
        self._last_history_page = initial.history_page

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        """Print state changes or a newly requested history page."""
        self._snapshot = snapshot
        if snapshot.dashboard_view is DashboardView.HISTORY and (
            self._last_view is not DashboardView.HISTORY
            or self._last_history_page != snapshot.history_page
        ):
            self.console.print(render_dashboard(snapshot, width=120))
            self._last_view = snapshot.dashboard_view
            self._last_history_page = snapshot.history_page
            return
        self._last_view = snapshot.dashboard_view
        self._last_history_page = snapshot.history_page
        if snapshot.state != self._last_state or snapshot.message != self._last_message:
            self.console.print(f"[{snapshot.state.value}] {snapshot.message}")
            self._last_state = snapshot.state
            self._last_message = snapshot.message

    def log(self, message: str, *, level: str = "info") -> None:
        """Print one structured human-readable log line."""
        self.console.print(f"{level.upper():7} {message}")

    def close(self) -> None:
        """Print a compact final session summary idempotently."""
        if self._closed:
            return
        self._closed = True
        snapshot = self._snapshot
        self.console.print(
            f"Session complete: {snapshot.session_fish} fish, "
            f"{snapshot.session_items} items, {snapshot.session_escapes} escapes, "
            f"{snapshot.session_treasure_collected} treasure secured, "
            f"{snapshot.session_treasure_looted} chest menus looted, "
            f"{snapshot.session_food_consumed} food consumed."
        )


def render_dashboard(
    snapshot: RuntimeSnapshot,
    events: tuple[tuple[str, str], ...] = (),
    width: int = 120,
) -> RenderableType:
    """Build a responsive current-task or historical-statistics render tree."""
    if (
        snapshot.dashboard_view is DashboardView.HISTORY
        and snapshot.historical_stats is not None
    ):
        return _render_history(snapshot, snapshot.historical_stats, width)
    title = Text(" REELPILOT ", style="bold black on bright_cyan")
    if snapshot.state is AutomationState.WAITING_FOR_GAME:
        status = "WAITING"
        controls = "F5 Stats  F6 Debug Start  F7 Start  F8 Stop"
    elif snapshot.state is AutomationState.READY:
        status = "READY"
        controls = "F5 Stats  F6 Debug Start  F7 Start  F8 Stop"
    elif snapshot.paused:
        status = "PAUSED"
        controls = "F5 Stats  F7 Resume  F8 Stop"
    else:
        status = "CONNECTED" if snapshot.connected else "WAITING"
        controls = "F7 Pause  F8 Stop"
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right")
    header.add_row(title, f"[bold]{status}[/]  {controls}")

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
    margin = (
        f"{snapshot.containment_margin_pixels:+.1f}px"
        if snapshot.containment_margin_pixels is not None
        else "unknown"
    )
    current.add_row(
        "Perfect",
        f"{snapshot.perfect_status.value} / {snapshot.control_phase.value}  margin {margin}",
    )
    current.add_row("Treasure", snapshot.treasure_status.value)
    energy = (
        f"{snapshot.energy_ratio:.0%}"
        if snapshot.energy_ratio is not None
        else "unread"
    )
    current.add_row("Energy", f"{energy} / {snapshot.energy_status.value}")
    if snapshot.bite_seconds_remaining is not None:
        current.add_row("Bite", f"{snapshot.bite_seconds_remaining:0.1f}s remaining")
    if snapshot.start_mode is not None:
        current.add_row("Start", snapshot.start_mode.value)
    if snapshot.recording_path is not None:
        used = _format_bytes(snapshot.recorded_image_bytes)
        limit = _format_bytes(snapshot.recording_image_limit_bytes)
        current.add_row("Debug", f"recording {used} / {limit}")
        if snapshot.dropped_images:
            current.add_row("Images", f"{snapshot.dropped_images} dropped/capped")
        if snapshot.debug_warnings:
            current.add_row("Warnings", str(snapshot.debug_warnings))

    stats = Table.grid(padding=(0, 1), expand=True)
    stats.add_column(style="green")
    stats.add_column(justify="right")
    stats.add_row("Session fish", str(snapshot.session_fish))
    stats.add_row("Other items", str(snapshot.session_items))
    stats.add_row("Escapes", str(snapshot.session_escapes))
    stats.add_row("Lifetime fish", str(snapshot.lifetime_fish))
    session_perfect = (
        snapshot.session_perfect / snapshot.session_perfect_attempts
        if snapshot.session_perfect_attempts
        else 0.0
    )
    stats.add_row(
        "Session Perfect",
        f"{snapshot.session_perfect}/{snapshot.session_perfect_attempts} ({session_perfect:.1%})",
    )
    stats.add_row(
        "Chests seen/secured",
        f"{snapshot.session_treasure_seen}/{snapshot.session_treasure_collected}",
    )
    stats.add_row("Chest menus looted", str(snapshot.session_treasure_looted))
    stats.add_row("Food consumed", str(snapshot.session_food_consumed))
    stats.add_row(
        "Inventory-full stops", str(snapshot.session_inventory_full_stops)
    )

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
    """Render a clamped Unicode progress bar."""
    ratio = max(0.0, min(1.0, value))
    filled = round(width * ratio)
    return f"[bright_green]{'━' * filled}[/][grey35]{'─' * (width - filled)}[/] {ratio:3.0%}"


def _format_bytes(value: int) -> str:
    """Format byte counts compactly for recording diagnostics."""
    if value >= 1024**3:
        return f"{value / 1024**3:0.1f} GB"
    if value >= 1024**2:
        return f"{value / 1024**2:0.1f} MB"
    if value >= 1024:
        return f"{value / 1024:0.1f} KB"
    return f"{value} B"


def _render_history(
    snapshot: RuntimeSnapshot,
    history: HistoricalStatsSnapshot,
    width: int,
) -> RenderableType:
    """Render lifetime aggregates and one paged species table."""
    title = Text(" REELPILOT HISTORY ", style="bold black on bright_green")
    action = "F7 Resume" if snapshot.paused else "F7 Start"
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right")
    header.add_row(
        title,
        f"[bold]HISTORY[/]  F5 Back  PgUp/PgDn Species  {action}  F8 Stop",
    )

    totals = Table.grid(padding=(0, 1), expand=True)
    totals.add_column(style="green")
    totals.add_column(justify="right")
    totals.add_row("Sessions", str(history.sessions))
    totals.add_row("Runtime", _format_duration(history.runtime_milliseconds))
    totals.add_row("Casts / bites", f"{history.casts} / {history.bites}")
    totals.add_row("Fish / items", f"{history.fish} / {history.items}")
    totals.add_row("Unknown", str(history.unknown_results))
    totals.add_row("Escapes", str(history.escapes))
    totals.add_row("Timeouts / aborted", f"{history.timeouts} / {history.aborted}")
    totals.add_row("Fish success", f"{history.success_ratio:.1%}")
    totals.add_row(
        "Verified Perfect",
        f"{history.perfect_confirmed}/{history.perfect_confirmed + history.perfect_missed} "
        f"({history.perfect_ratio:.1%}); {history.perfect_unknown} unknown",
    )
    totals.add_row(
        "Verified MAX",
        f"{history.max_verified}/{history.max_attempts} ({history.max_verified_ratio:.1%})",
    )
    totals.add_row(
        "Treasure chests",
        f"{history.treasure_collected}/{history.treasure_seen} secured "
        f"({history.treasure_collection_ratio:.1%}); {history.treasure_attempts} attempts",
    )
    totals.add_row(
        "Loot menus",
        f"{history.treasure_looted}/{history.treasure_collected} transferred "
        f"({history.treasure_loot_ratio:.1%})",
    )
    totals.add_row("Food consumed", str(history.food_consumed))
    totals.add_row("Inventory-full stops", str(history.inventory_full_stops))
    if history.minimum_energy_ratio is not None:
        totals.add_row("Lowest energy", f"{history.minimum_energy_ratio:.0%}")
    totals.add_row(
        "Estimated value",
        f"{history.estimated_minimum_value_gold:,}–{history.estimated_maximum_value_gold:,}g",
    )

    session_table = Table(expand=True, box=None, padding=(0, 1))
    session_table.add_column("Started", style="cyan")
    session_table.add_column("Run", justify="right")
    session_table.add_column("Fish", justify="right")
    session_table.add_column("Items", justify="right")
    session_table.add_column("Esc", justify="right")
    session_table.add_column("Stop")
    for session_row in history.recent_sessions[:5]:
        session_table.add_row(
            session_row.started_at_utc.replace("T", " ")[:16],
            _format_duration(session_row.runtime_milliseconds),
            str(session_row.fish),
            str(session_row.items),
            str(session_row.escapes),
            session_row.stop_reason or "active",
        )
    if not history.recent_sessions:
        session_table.add_row("No sessions recorded", "", "", "", "", "")

    page_size = 8
    maximum_page = max(0, (len(history.species) - 1) // page_size)
    page = min(maximum_page, max(0, snapshot.history_page))
    species_table = Table(expand=True, box=None, padding=(0, 1))
    species_table.add_column("Species", style="magenta")
    species_table.add_column("Qty", justify="right")
    species_table.add_column("Share", justify="right")
    species_table.add_column("Difficulty")
    species_table.add_column("Length", justify="right")
    species_table.add_column("Base", justify="right")
    species_table.add_column("Lifetime est.", justify="right")
    for species_row in history.species[page * page_size : (page + 1) * page_size]:
        lengths = "—"
        if (
            species_row.minimum_length_inches is not None
            and species_row.maximum_length_inches is not None
        ):
            average = species_row.average_length_inches or 0.0
            lengths = (
                f"{species_row.minimum_length_inches}/{average:.1f}/"
                f"{species_row.maximum_length_inches} in."
            )
        species_table.add_row(
            species_row.name,
            str(species_row.quantity),
            f"{species_row.observed_share_ratio:.1%}",
            species_row.difficulty_tier.value if species_row.difficulty_tier else "—",
            lengths,
            (
                f"{species_row.base_sell_price_gold:,}g"
                if species_row.base_sell_price_gold is not None
                else "—"
            ),
            f"{species_row.estimated_minimum_value_gold:,}–"
            f"{species_row.estimated_maximum_value_gold:,}g",
        )
    if not history.species:
        species_table.add_row("No recognized fish yet", "", "", "", "", "", "")

    catches = Table.grid(expand=True)
    catches.add_column()
    for label in history.recent_catches[:6]:
        catches.add_row(f"• {label}")
    if not history.recent_catches:
        catches.add_row("[dim]No catches recorded yet[/]")

    species_title = f"Species — page {page + 1}/{maximum_page + 1}"
    if width >= 100:
        upper = Table.grid(expand=True)
        upper.add_column(ratio=1)
        upper.add_column(ratio=2)
        upper.add_row(
            Panel(totals, title="Lifetime", border_style="green"),
            Panel(session_table, title="Recent sessions", border_style="cyan"),
        )
        content: RenderableType = Group(
            upper,
            Panel(species_table, title=species_title, border_style="magenta"),
            Panel(catches, title="Recent catches", border_style="blue"),
        )
    else:
        content = Group(
            Panel(totals, title="Lifetime", border_style="green"),
            Panel(species_table, title=species_title, border_style="magenta"),
            Panel(catches, title="Recent catches", border_style="blue"),
        )
    return Group(Panel(header, border_style="bright_green"), content)


def _format_duration(milliseconds: int) -> str:
    """Format a non-negative duration as hours and minutes or seconds."""
    seconds = max(0, milliseconds) // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {remaining_seconds:02d}s"
    return f"{remaining_seconds}s"
