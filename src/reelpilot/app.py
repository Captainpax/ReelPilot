"""Top-level ReelPilot application composition and safe startup lifecycle."""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import datetime
from logging.handlers import QueueListener
from pathlib import Path

from .automation import AutomationEngine
from .domain import (
    AutomationState,
    DashboardView,
    ReelPilotSettings,
    RuntimeSnapshot,
    StartMode,
)
from .input import InputController
from .platform.paths import application_data_directory
from .platform.windows import (
    debug_start_requested,
    find_stardew_window,
    next_stats_page_requested,
    pause_requested,
    previous_stats_page_requested,
    stats_requested,
    stop_requested,
)
from .protocols import DashboardPort
from .recording import ReplayRunner, SessionRecorder
from .stats import SQLiteStatsRepository, StatsService
from .ui import PlainDashboard, RichDashboard
from .ui.logging import configure_logging
from .vision import VisionPipeline


class ReelPilotApplication:
    """Compose platform, vision, input, statistics, recording, and dashboard services."""

    def __init__(self, settings: ReelPilotSettings) -> None:
        """Validate and retain settings without touching the game or filesystem."""
        settings.validate()
        self.settings = settings
        self.data_directory = application_data_directory()
        self.logger: logging.Logger | None = None
        self.log_listener: QueueListener | None = None

    def run(self) -> int:
        """Run ReelPilot until a safe stop, timeout, or recoverable error."""
        self.logger, self.log_listener = configure_logging(self.data_directory / "logs")
        initial = RuntimeSnapshot(
            AutomationState.WAITING_FOR_GAME,
            0.0,
            False,
            False,
            self.settings.automation_mode,
            self.settings.controller_profile,
            message="Looking for Stardew Valley",
        )
        use_plain = self.settings.plain or not sys.stdout.isatty()
        dashboard = PlainDashboard(initial) if use_plain else RichDashboard(initial)
        vision: VisionPipeline | None = None
        repository: SQLiteStatsRepository | None = None
        stats: StatsService | None = None
        recorder: SessionRecorder | None = None
        session_id: str | None = None
        started_seconds: float | None = None
        stop_reason = "normal"
        start_mode: StartMode | None = None
        try:
            if self.settings.stats_enabled:
                try:
                    stats_root = self.settings.stats_directory or self.data_directory / "stats"
                    repository = SQLiteStatsRepository(stats_root)
                except (OSError, ValueError, sqlite3.Error) as exc:
                    repository = None
                    dashboard.log(f"Statistics disabled: {exc}", level="warning")
                    self.logger.exception("statistics initialization failed")
            start = self._wait_for_start(dashboard, repository)
            if start is None:
                stop_reason = "f8"
                return 0
            window_handle, start_mode = start
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            started_seconds = time.monotonic()
            settings_dict = self._settings_dict(start_mode)
            vision = VisionPipeline(window_handle)
            if repository is not None:
                stats = StatsService(repository, session_id, settings_dict)
            record_root = self._recording_root(start_mode)
            if record_root is not None:
                recorder = SessionRecorder(record_root, settings_dict)
                dashboard.log(f"Recording diagnostics to {recorder.path}")
            dashboard.log(f"Starting {start_mode.value} automation")
            assert vision.window_handle is not None
            with InputController(vision.window_handle) as input_controller:
                engine = AutomationEngine(
                    self.settings,
                    vision,
                    dashboard,
                    stats=stats,
                    recorder=recorder,
                    start_mode=start_mode,
                    stop_requested=stop_requested,
                    pause_requested=pause_requested,
                    debug_requested=debug_start_requested,
                    stats_requested=stats_requested,
                    previous_stats_page_requested=previous_stats_page_requested,
                    next_stats_page_requested=next_stats_page_requested,
                )
                outcome = engine.run(input_controller)
                stop_reason = outcome.value
                return 0 if outcome.value == "stopped" else 1
        except KeyboardInterrupt:
            stop_reason = "ctrl-c"
            dashboard.log("Ctrl+C received; input released")
            return 0
        except (OSError, RuntimeError, ValueError) as exc:
            stop_reason = "error"
            dashboard.log(str(exc), level="error")
            self.logger.exception("ReelPilot stopped with an error")
            return 1
        finally:
            # InputController's context has already released input before any
            # capture or writer is closed.
            if vision is not None:
                vision.close()
            if (
                stats is not None
                and repository is not None
                and session_id is not None
                and started_seconds is not None
            ):
                repository.close_session(
                    session_id,
                    stop_reason,
                    round((time.monotonic() - started_seconds) * 1000),
                )
            if recorder is not None:
                recorder.close(timeout_seconds=2.0)
                if recorder.image_limit_reached:
                    dashboard.log("Debug image limit reached; telemetry remained active", level="warning")
                if start_mode is StartMode.DEBUG and recorder.writer_complete:
                    try:
                        ReplayRunner().run(recorder.path, self.settings.controller_profile)
                        dashboard.log(f"Debug report written to {recorder.path / 'report.json'}")
                    except (OSError, RuntimeError, ValueError) as exc:
                        dashboard.log(f"Debug report failed: {exc}", level="warning")
                elif start_mode is StartMode.DEBUG:
                    dashboard.log(
                        "Debug writer exceeded the two-second shutdown bound; "
                        "the manifest is marked incomplete",
                        level="warning",
                    )
            if repository is not None:
                repository.close(timeout_seconds=2.0)
            dashboard.close()
            if self.log_listener is not None:
                self.log_listener.stop()

    def _wait_for_start(
        self,
        dashboard: DashboardPort,
        repository: SQLiteStatsRepository | None = None,
    ) -> tuple[int, StartMode] | None:
        """Wait safely for a game window and an explicit F6/F7 start request."""
        state = AutomationState.WAITING_FOR_GAME
        state_started = time.monotonic()
        window_handle: int | None = None
        next_scan_seconds = 0.0
        last_message = ""
        view = DashboardView.CURRENT
        history = None
        history_page = 0
        lifetime = repository.load_snapshot() if repository is not None else None
        while True:
            if stop_requested():
                dashboard.log("F8 received before start")
                dashboard.publish(
                    RuntimeSnapshot(
                        AutomationState.STOPPED,
                        0.0,
                        window_handle is not None,
                        False,
                        self.settings.automation_mode,
                        self.settings.controller_profile,
                        message="Stopped safely before automation started",
                    )
                )
                return None
            now = time.monotonic()
            if stats_requested():
                if window_handle is None:
                    dashboard.log("F5 Stats is available after Stardew is detected")
                elif repository is None:
                    dashboard.log("Statistics are disabled", level="warning")
                elif view is DashboardView.HISTORY:
                    view = DashboardView.CURRENT
                else:
                    try:
                        history = repository.refresh_history(timeout_seconds=2.0)
                        lifetime = repository.load_snapshot()
                        history_page = 0
                        view = DashboardView.HISTORY
                        dashboard.log("Historical statistics refreshed")
                    except (OSError, sqlite3.Error, TimeoutError) as exc:
                        dashboard.log(f"Statistics refresh failed: {exc}", level="warning")
            if view is DashboardView.HISTORY and history is not None:
                maximum_page = max(0, (len(history.species) - 1) // 8)
                if previous_stats_page_requested():
                    history_page = max(0, history_page - 1)
                if next_stats_page_requested():
                    history_page = min(maximum_page, history_page + 1)
            if now >= next_scan_seconds:
                detected = find_stardew_window()
                next_scan_seconds = now + 0.5
                if detected != window_handle:
                    window_handle = detected
                    state = (
                        AutomationState.READY
                        if window_handle is not None
                        else AutomationState.WAITING_FOR_GAME
                    )
                    state_started = now
            if window_handle is None:
                # Consume stale edges so pressing a start key before the game
                # connects cannot arm an automatic future start.
                debug_start_requested()
                pause_requested()
                message = "Waiting for Stardew Valley"
            else:
                message = "Ready — press F7 to start or F6 for debug"
                if debug_start_requested():
                    return window_handle, StartMode.DEBUG
                if pause_requested():
                    return window_handle, StartMode.NORMAL
            if message != last_message:
                dashboard.log(message)
                last_message = message
            dashboard.publish(
                RuntimeSnapshot(
                    state=state,
                    state_elapsed_seconds=now - state_started,
                    connected=window_handle is not None,
                    paused=False,
                    automation_mode=self.settings.automation_mode,
                    controller_profile=self.settings.controller_profile,
                    lifetime_fish=lifetime.lifetime_fish if lifetime is not None else 0,
                    lifetime_treasure_seen=(
                        lifetime.lifetime_treasure_seen if lifetime is not None else 0
                    ),
                    lifetime_treasure_collected=(
                        lifetime.lifetime_treasure_collected if lifetime is not None else 0
                    ),
                    lifetime_treasure_looted=(
                        lifetime.lifetime_treasure_looted if lifetime is not None else 0
                    ),
                    lifetime_food_consumed=(
                        lifetime.lifetime_food_consumed if lifetime is not None else 0
                    ),
                    lifetime_inventory_full_stops=(
                        lifetime.lifetime_inventory_full_stops
                        if lifetime is not None
                        else 0
                    ),
                    recent_catches=lifetime.recent_catches if lifetime is not None else (),
                    message=message,
                    dashboard_view=view,
                    historical_stats=history,
                    history_page=history_page,
                )
            )
            time.sleep(0.05)

    def _settings_dict(self, start_mode: StartMode) -> dict[str, object]:
        """Return JSON-safe settings stored in session and recording manifests."""
        return {
            "automation_mode": self.settings.automation_mode.value,
            "controller_profile": self.settings.controller_profile.value,
            "fishing_level": self.settings.fishing_level,
            "cast_hold_seconds": self.settings.cast_hold_seconds,
            "cast_hold_seconds_explicit": self.settings.cast_hold_seconds_explicit,
            "stats_enabled": self.settings.stats_enabled,
            "rod_slot": self.settings.rod_slot,
            "food_slot": self.settings.food_slot,
            "auto_eat": self.settings.auto_eat,
            "start_mode": start_mode.value,
        }

    def _recording_root(self, start_mode: StartMode) -> Path | None:
        """Choose the explicit or default diagnostic recording root."""
        if self.settings.record_directory is not None:
            return self.settings.record_directory
        if start_mode is StartMode.DEBUG:
            return self.data_directory / "recordings"
        return None
