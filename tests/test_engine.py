from reelpilot.automation import AutomationEngine
from reelpilot.domain import (
    AutomationMode,
    AutomationState,
    CastObservation,
    CatchObservation,
    FishingObservation,
    ReelPilotSettings,
    RuntimeSnapshot,
)


class FakeVision:
    window_handle = 1

    def observe_scene(
        self, expected_bar_length_pixels: int | None = None
    ) -> FishingObservation:
        return FishingObservation(False)

    def begin_cast(self) -> None:
        pass

    def observe_cast(self) -> CastObservation:
        return CastObservation(False)

    def detect_bite(self) -> bool:
        return False

    def read_catch(self) -> CatchObservation:
        return CatchObservation(False)

    def latest_catch_card(self) -> None:
        return None

    def close(self) -> None:
        pass


class FakeInput:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def set_duty(self, duty_ratio: float) -> None:
        self.operations.append(f"duty:{duty_ratio:.2f}")

    def press(self) -> None:
        self.operations.append("press")

    def release(self) -> None:
        self.operations.append("release")

    def tap(self, duration_seconds: float = 0.04) -> None:
        self.operations.append("tap")

    def idle(self) -> None:
        self.operations.append("idle")

    def close(self) -> None:
        self.operations.append("close")


class FakeDashboard:
    def __init__(self) -> None:
        self.snapshots: list[RuntimeSnapshot] = []

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self.snapshots.append(snapshot)

    def log(self, message: str, *, level: str = "info") -> None:
        pass

    def close(self) -> None:
        pass


def test_f8_stops_before_any_input_and_idles_in_finally() -> None:
    hand = FakeInput()
    dashboard = FakeDashboard()
    engine = AutomationEngine(
        ReelPilotSettings(automation_mode=AutomationMode.MINIGAME_ONLY),
        FakeVision(),
        dashboard,
        stop_requested=lambda: True,
        pause_requested=lambda: False,
        sleep=lambda _: None,
    )
    result = engine.run(hand)
    assert result.value == "stopped"
    assert hand.operations[-1] == "idle"
    assert dashboard.snapshots[-1].state is AutomationState.STOPPED
