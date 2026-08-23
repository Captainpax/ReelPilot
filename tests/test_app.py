from reelpilot import app as app_module
from reelpilot.app import ReelPilotApplication
from reelpilot.domain import (
    AutomationState,
    DashboardView,
    ReelPilotSettings,
    RuntimeSnapshot,
    StartMode,
)
from reelpilot.stats import HistoricalStatsSnapshot, StatsSnapshot


class Dashboard:
    def __init__(self) -> None:
        self.snapshots: list[RuntimeSnapshot] = []

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self.snapshots.append(snapshot)

    def log(self, message: str, *, level: str = "info") -> None:
        pass

    def close(self) -> None:
        pass


class Repository:
    def __init__(self) -> None:
        self.refreshes = 0

    def load_snapshot(self, session_id: str | None = None) -> StatsSnapshot:
        return StatsSnapshot(
            lifetime_fish=7,
            lifetime_treasure_seen=5,
            lifetime_treasure_collected=4,
            lifetime_treasure_looted=3,
            lifetime_food_consumed=9,
            lifetime_inventory_full_stops=2,
        )

    def refresh_history(self, timeout_seconds: float = 2.0) -> HistoricalStatsSnapshot:
        self.refreshes += 1
        return HistoricalStatsSnapshot(sessions=3, fish=7)


def test_f7_selects_normal_start_only_after_game_is_ready(monkeypatch) -> None:
    dashboard = Dashboard()
    monkeypatch.setattr(app_module, "find_stardew_window", lambda: 123)
    monkeypatch.setattr(app_module, "stop_requested", lambda: False)
    monkeypatch.setattr(app_module, "debug_start_requested", lambda: False)
    monkeypatch.setattr(app_module, "pause_requested", lambda: True)

    result = ReelPilotApplication(ReelPilotSettings())._wait_for_start(dashboard)

    assert result == (123, StartMode.NORMAL)


def test_f6_selects_debug_start(monkeypatch) -> None:
    dashboard = Dashboard()
    monkeypatch.setattr(app_module, "find_stardew_window", lambda: 321)
    monkeypatch.setattr(app_module, "stop_requested", lambda: False)
    monkeypatch.setattr(app_module, "debug_start_requested", lambda: True)
    monkeypatch.setattr(app_module, "pause_requested", lambda: True)

    result = ReelPilotApplication(ReelPilotSettings())._wait_for_start(dashboard)

    assert result == (321, StartMode.DEBUG)


def test_f8_stops_safely_while_waiting_for_game(monkeypatch) -> None:
    dashboard = Dashboard()
    monkeypatch.setattr(app_module, "find_stardew_window", lambda: None)
    monkeypatch.setattr(app_module, "stop_requested", lambda: True)

    result = ReelPilotApplication(ReelPilotSettings())._wait_for_start(dashboard)

    assert result is None
    assert dashboard.snapshots[-1].state is AutomationState.STOPPED


def test_debug_start_uses_default_local_recording_directory() -> None:
    application = ReelPilotApplication(ReelPilotSettings())

    assert application._recording_root(StartMode.NORMAL) is None
    assert application._recording_root(StartMode.DEBUG) == (
        application.data_directory / "recordings"
    )


def test_f5_refreshes_history_from_ready_screen(monkeypatch) -> None:
    dashboard = Dashboard()
    repository = Repository()
    stats_edges = iter((False, True, False, False))
    start_edges = iter((False, False, True))
    monkeypatch.setattr(app_module, "find_stardew_window", lambda: 123)
    monkeypatch.setattr(app_module, "stop_requested", lambda: False)
    monkeypatch.setattr(app_module, "debug_start_requested", lambda: False)
    monkeypatch.setattr(app_module, "stats_requested", lambda: next(stats_edges, False))
    monkeypatch.setattr(app_module, "pause_requested", lambda: next(start_edges, True))
    monkeypatch.setattr(app_module, "previous_stats_page_requested", lambda: False)
    monkeypatch.setattr(app_module, "next_stats_page_requested", lambda: False)
    monkeypatch.setattr(app_module.time, "sleep", lambda _: None)

    result = ReelPilotApplication(ReelPilotSettings())._wait_for_start(
        dashboard, repository  # type: ignore[arg-type]
    )

    assert result == (123, StartMode.NORMAL)
    assert repository.refreshes == 1
    assert any(
        snapshot.dashboard_view is DashboardView.HISTORY
        and snapshot.historical_stats is not None
        for snapshot in dashboard.snapshots
    )
    ready_snapshot = next(
        snapshot
        for snapshot in dashboard.snapshots
        if snapshot.state is AutomationState.READY
    )
    assert ready_snapshot.lifetime_treasure_seen == 5
    assert ready_snapshot.lifetime_treasure_collected == 4
    assert ready_snapshot.lifetime_treasure_looted == 3
    assert ready_snapshot.lifetime_food_consumed == 9
    assert ready_snapshot.lifetime_inventory_full_stops == 2
