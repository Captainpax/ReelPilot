from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import datetime
from logging.handlers import QueueListener

from .automation import AutomationEngine
from .domain import AutomationState, ReelPilotSettings, RuntimeSnapshot
from .input import InputController
from .platform.paths import application_data_directory
from .platform.windows import pause_requested, stop_requested
from .recording import SessionRecorder
from .stats import SQLiteStatsRepository, StatsService
from .ui import PlainDashboard, RichDashboard
from .ui.logging import configure_logging
from .vision import VisionPipeline


class ReelPilotApplication:
    def __init__(self, settings: ReelPilotSettings) -> None:
        settings.validate()
        self.settings = settings
        self.data_directory = application_data_directory()
        self.logger: logging.Logger | None = None
        self.log_listener: QueueListener | None = None

    def run(self) -> int:
        self.logger, self.log_listener = configure_logging(self.data_directory / "logs")
        initial = RuntimeSnapshot(
            AutomationState.STARTUP,
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
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        started_seconds = time.monotonic()
        stop_reason = "normal"
        try:
            vision = VisionPipeline()
            settings_dict = self._settings_dict()
            if self.settings.stats_enabled:
                try:
                    stats_root = self.settings.stats_directory or self.data_directory / "stats"
                    repository = SQLiteStatsRepository(stats_root)
                    stats = StatsService(repository, session_id, settings_dict)
                except (OSError, ValueError, sqlite3.Error) as exc:
                    repository = None
                    dashboard.log(f"Statistics disabled: {exc}", level="warning")
                    self.logger.exception("statistics initialization failed")
            if self.settings.record_directory is not None:
                recorder = SessionRecorder(self.settings.record_directory, settings_dict)
            dashboard.log("Stardew Valley detected")
            assert vision.window_handle is not None
            with InputController(vision.window_handle) as input_controller:
                engine = AutomationEngine(
                    self.settings,
                    vision,
                    dashboard,
                    stats=stats,
                    recorder=recorder,
                    stop_requested=stop_requested,
                    pause_requested=pause_requested,
                )
                outcome = engine.run(input_controller)
                stop_reason = outcome.value
                return 1 if outcome.value == "bite-timeout" else 0
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
            if stats is not None and repository is not None:
                repository.close_session(
                    session_id,
                    stop_reason,
                    round((time.monotonic() - started_seconds) * 1000),
                )
            if recorder is not None:
                recorder.close(timeout_seconds=2.0)
            if repository is not None:
                repository.close(timeout_seconds=2.0)
            dashboard.close()
            if self.log_listener is not None:
                self.log_listener.stop()

    def _settings_dict(self) -> dict[str, object]:
        return {
            "automation_mode": self.settings.automation_mode.value,
            "controller_profile": self.settings.controller_profile.value,
            "fishing_level": self.settings.fishing_level,
            "cast_hold_seconds": self.settings.cast_hold_seconds,
            "stats_enabled": self.settings.stats_enabled,
        }
