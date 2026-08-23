from dataclasses import replace

from reelpilot.automation import AutomationEngine, EngineResult
from reelpilot.domain import (
    AutomationMode,
    AutomationState,
    BiteObservation,
    CastObservation,
    CatchObservation,
    ControlPhase,
    DashboardView,
    DifficultyTier,
    EncounterOutcome,
    EnergyObservation,
    FishingObservation,
    FoodPromptObservation,
    PerfectStatus,
    RecognitionStatus,
    ReelPilotSettings,
    ResultType,
    RuntimeSnapshot,
    TreasureLootObservation,
    TreasureStatus,
)
from reelpilot.stats import HistoricalStatsSnapshot, SpeciesStats, StatsSnapshot


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

    def observe_bite(self) -> BiteObservation:
        return BiteObservation(False)

    def detect_text(self, expected_text: str) -> bool:
        return False

    def read_catch(self) -> CatchObservation:
        return CatchObservation(False)

    def observe_treasure_loot(self) -> TreasureLootObservation:
        return TreasureLootObservation(False)

    def observe_energy(self) -> EnergyObservation:
        return EnergyObservation(True, 1.0, 1.0)

    def observe_food_prompt(self) -> FoodPromptObservation:
        return FoodPromptObservation(False)

    def latest_frame(self) -> None:
        return None

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

    def prepare_menu_capture(self) -> None:
        pass

    def click_at(self, x_pixels: int, y_pixels: int) -> None:
        self.operations.append(f"click:{x_pixels},{y_pixels}")

    def right_click_at(self, x_pixels: int, y_pixels: int) -> None:
        self.operations.append(f"right-click:{x_pixels},{y_pixels}")

    def tap_key(self, virtual_key: int) -> None:
        self.operations.append(f"key:{virtual_key}")

    def close(self) -> None:
        self.operations.append("close")


class FakeDashboard:
    def __init__(self) -> None:
        self.snapshots: list[RuntimeSnapshot] = []
        self.messages: list[str] = []

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self.snapshots.append(snapshot)

    def log(self, message: str, *, level: str = "info") -> None:
        self.messages.append(message)

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


class AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.1
        return self.value


def make_engine(vision: FakeVision, clock=None) -> AutomationEngine:
    return AutomationEngine(
        ReelPilotSettings(automation_mode=AutomationMode.MINIGAME_ONLY),
        vision,
        FakeDashboard(),
        stop_requested=lambda: False,
        pause_requested=lambda: False,
        clock=clock or AdvancingClock(),
        sleep=lambda _: None,
    )


def test_result_card_takes_precedence_over_low_progress() -> None:
    vision = FakeVision()
    vision.read_catch = lambda: CatchObservation(  # type: ignore[method-assign]
        True,
        ResultType.FISH,
        "Anchovy",
        14,
        confidence=0.95,
        status=RecognitionStatus.RECOGNIZED,
    )
    engine = make_engine(vision)
    engine._begin_encounter_if_needed()
    assert engine._encounter is not None
    engine._encounter.peak_progress_ratio = 0.4
    engine._encounter.peak_progress_confidence = 1.0

    assert engine._collect_result(FakeInput())
    assert engine._encounter.outcome is EncounterOutcome.FISH


def test_reliable_low_progress_without_card_is_escape_and_not_dismissed() -> None:
    engine = make_engine(FakeVision(), AdvancingClock())
    engine._begin_encounter_if_needed()
    assert engine._encounter is not None
    engine._encounter.peak_progress_ratio = 0.5
    engine._encounter.peak_progress_confidence = 0.9

    assert not engine._collect_result(FakeInput())
    assert engine._encounter.outcome is EncounterOutcome.ESCAPED


def test_near_full_progress_without_card_is_still_escape() -> None:
    engine = make_engine(FakeVision(), AdvancingClock())
    engine._begin_encounter_if_needed()
    assert engine._encounter is not None
    engine._encounter.peak_progress_ratio = 0.968
    engine._encounter.peak_progress_confidence = 1.0

    assert not engine._collect_result(FakeInput())
    assert engine._encounter.outcome is EncounterOutcome.ESCAPED


def test_bite_timeout_closes_the_encounter_row() -> None:
    vision = FakeVision()
    clock = AdvancingClock()
    engine = make_engine(vision, clock)
    engine._begin_encounter_if_needed()
    updates: list[tuple[str, object]] = []
    engine._update_encounter = lambda field, value: updates.append(  # type: ignore[method-assign]
        (field, value)
    )
    engine._bite_deadline_seconds = clock()

    assert not engine._wait_for_bite(FakeInput())
    assert ("outcome", EncounterOutcome.TIMED_OUT.value) in updates
    assert any(field == "ended_at_utc" for field, _value in updates)


def test_catch_registration_is_latched_per_encounter() -> None:
    engine = make_engine(FakeVision())
    engine._begin_encounter_if_needed()
    catch = CatchObservation(True, ResultType.ITEM, "Seaweed")

    engine._register_catch(catch)
    engine._register_catch(catch)

    assert engine._encounter is not None
    assert engine._encounter.result_registered
    assert engine._encounter.outcome is EncounterOutcome.ITEM


def test_f5_is_rejected_without_changing_active_automation() -> None:
    dashboard = FakeDashboard()
    engine = AutomationEngine(
        ReelPilotSettings(automation_mode=AutomationMode.MINIGAME_ONLY),
        FakeVision(),
        dashboard,
        stop_requested=lambda: False,
        pause_requested=lambda: False,
        stats_requested=lambda: True,
        sleep=lambda _: None,
    )

    engine._check_hotkeys(FakeInput())

    assert engine.state is AutomationState.STARTUP
    assert dashboard.messages[-1] == "Pause with F7 before viewing statistics"


class FakeStats:
    snapshot = StatsSnapshot()

    def refresh_history(self, timeout_seconds: float = 2.0) -> HistoricalStatsSnapshot:
        return HistoricalStatsSnapshot(sessions=2, fish=3)


def test_paused_f5_toggles_refreshed_history() -> None:
    dashboard = FakeDashboard()
    engine = AutomationEngine(
        ReelPilotSettings(automation_mode=AutomationMode.MINIGAME_ONLY),
        FakeVision(),
        dashboard,
        stats=FakeStats(),  # type: ignore[arg-type]
        stop_requested=lambda: False,
        pause_requested=lambda: False,
        sleep=lambda _: None,
    )

    engine._toggle_stats_history()
    assert engine._dashboard_view is DashboardView.HISTORY
    assert engine._historical_stats is not None
    assert engine._historical_stats.fish == 3

    engine._toggle_stats_history()
    assert engine._dashboard_view is DashboardView.CURRENT


def test_paused_f5_reports_disabled_statistics() -> None:
    dashboard = FakeDashboard()
    engine = AutomationEngine(
        ReelPilotSettings(
            automation_mode=AutomationMode.MINIGAME_ONLY,
            stats_enabled=False,
        ),
        FakeVision(),
        dashboard,
        stop_requested=lambda: False,
        pause_requested=lambda: False,
        sleep=lambda _: None,
    )

    engine._toggle_stats_history()

    assert dashboard.messages[-1] == "Statistics are disabled"
    assert engine._dashboard_view is DashboardView.CURRENT


def test_pause_records_a_result_card_that_appears_during_a_fight() -> None:
    vision = FakeVision()
    vision.read_catch = lambda: CatchObservation(  # type: ignore[method-assign]
        True,
        ResultType.FISH,
        "Sardine",
        12,
        confidence=0.95,
        status=RecognitionStatus.RECOGNIZED,
    )
    pause_edges = iter((False, True))
    engine = AutomationEngine(
        ReelPilotSettings(automation_mode=AutomationMode.CONTINUOUS),
        vision,
        FakeDashboard(),
        stop_requested=lambda: False,
        pause_requested=lambda: next(pause_edges),
        clock=AdvancingClock(),
        sleep=lambda _: None,
    )
    engine.state = AutomationState.FISHING
    engine._begin_encounter_if_needed()

    resumed_state = engine._pause(FakeInput())

    assert engine._encounter is not None
    assert engine._encounter.result_registered
    assert engine._encounter.outcome is EncounterOutcome.FISH
    assert resumed_state is AutomationState.READING_RESULT


def test_history_page_navigation_is_clamped() -> None:
    row = SpeciesStats(
        "Bullhead",
        1,
        1,
        1.0,
        DifficultyTier.MEDIUM,
        31,
        31.0,
        31,
        75,
        75,
        225,
        "2026-08-22T00:00:00+00:00",
    )
    engine = AutomationEngine(
        ReelPilotSettings(automation_mode=AutomationMode.MINIGAME_ONLY),
        FakeVision(),
        FakeDashboard(),
        stop_requested=lambda: False,
        pause_requested=lambda: False,
        previous_stats_page_requested=lambda: False,
        next_stats_page_requested=lambda: True,
        sleep=lambda _: None,
    )
    engine._dashboard_view = DashboardView.HISTORY
    engine._historical_stats = HistoricalStatsSnapshot(species=(row,) * 9)

    engine._change_history_page()
    engine._change_history_page()

    assert engine._history_page == 1


def test_adaptive_cast_fallback_uses_verified_median_and_explicit_override() -> None:
    adaptive = make_engine(FakeVision())
    adaptive._verified_max_hold_seconds.extend((1.01, 1.08, 1.12, 1.18, 1.20))
    assert adaptive._fallback_hold_seconds() == 1.12

    explicit = AutomationEngine(
        ReelPilotSettings(
            automation_mode=AutomationMode.CONTINUOUS,
            cast_hold_seconds=1.37,
            cast_hold_seconds_explicit=True,
        ),
        FakeVision(),
        FakeDashboard(),
        stop_requested=lambda: False,
        pause_requested=lambda: False,
    )
    assert explicit._fallback_hold_seconds() == 1.37


def test_reliable_containment_break_switches_to_recovery_once() -> None:
    engine = make_engine(FakeVision())
    engine._begin_encounter_if_needed()
    escaped_bar = FishingObservation(
        True,
        100,
        150,
        96,
        fish_confidence=1.0,
        bar_confidence=1.0,
        fish_top_y_pixels=89,
        fish_bottom_y_pixels=111,
        bar_top_y_pixels=102,
        bar_bottom_y_pixels=198,
        containment_margin_pixels=-13,
    )

    engine._update_perfect_eligibility(escaped_bar)
    engine._update_perfect_eligibility(escaped_bar)

    assert engine._perfect_status is PerfectStatus.MISSED
    assert engine._encounter is not None
    assert engine._encounter.containment_breaks == 1


def test_shallow_edge_jitter_needs_progress_regression_to_break_perfect() -> None:
    engine = make_engine(FakeVision())
    engine._begin_encounter_if_needed()
    jitter = FishingObservation(
        True,
        352,
        391,
        92,
        progress_ratio=0.45,
        fish_confidence=0.9,
        bar_confidence=1.0,
        progress_confidence=1.0,
        fish_top_y_pixels=341,
        fish_bottom_y_pixels=363,
        bar_top_y_pixels=345,
        bar_bottom_y_pixels=437,
        containment_margin_pixels=-4,
    )

    engine._update_perfect_eligibility(jitter, peak_progress_ratio=0.45)
    assert engine._control_phase is ControlPhase.PERFECT

    engine._update_perfect_eligibility(jitter, peak_progress_ratio=0.46)
    assert engine._control_phase is ControlPhase.RECOVERY
    assert engine._perfect_status is PerfectStatus.MISSED


def test_bite_confirmation_requires_spatially_consistent_frames() -> None:
    vision = FakeVision()
    observations = iter(
        (
            BiteObservation(True, 0.9, icon_center_pixels=(100.0, 100.0)),
            BiteObservation(True, 0.9, icon_center_pixels=(110.0, 100.0)),
            BiteObservation(True, 0.9, icon_center_pixels=(111.0, 101.0)),
        )
    )
    vision.observe_bite = lambda: next(observations)  # type: ignore[method-assign]
    engine = make_engine(vision)
    engine._bite_deadline_seconds = engine.clock() + 45.0

    assert engine._wait_for_bite(FakeInput())


def treasure_observation(
    *, progress: float, fish_center: float = 300, bar_center: float = 300
) -> FishingObservation:
    return FishingObservation(
        True,
        fish_center,
        bar_center,
        100,
        progress,
        1.0,
        1.0,
        1.0,
        fish_top_y_pixels=fish_center - 11,
        fish_bottom_y_pixels=fish_center + 11,
        bar_top_y_pixels=bar_center - 50,
        bar_bottom_y_pixels=bar_center + 50,
        containment_margin_pixels=39,
        treasure_center_y_pixels=200,
        treasure_top_y_pixels=184,
        treasure_bottom_y_pixels=216,
        treasure_confidence=0.95,
    )


def test_treasure_attempt_spends_only_a_large_fish_progress_reserve() -> None:
    engine = make_engine(FakeVision())
    engine._begin_encounter_if_needed()

    target = engine._choose_treasure_target(
        treasure_observation(progress=0.99), 100, 1.0
    )
    aborted = engine._choose_treasure_target(
        treasure_observation(progress=0.65), 100, 1.1
    )

    assert target == 200
    assert aborted is None
    assert engine._treasure_status is TreasureStatus.SEEN


def test_treasure_target_can_contain_fish_and_chest_without_progress_reserve() -> None:
    engine = make_engine(FakeVision())
    engine._begin_encounter_if_needed()
    observation = treasure_observation(
        progress=0.20,
        fish_center=220,
        bar_center=220,
    )

    target = engine._choose_treasure_target(observation, 100, 1.0)

    assert target is not None
    assert 200 <= target <= 220
    assert engine._treasure_status is TreasureStatus.TARGETING


def test_treasure_never_starts_from_an_unsafe_fish_margin() -> None:
    engine = make_engine(FakeVision())
    engine._begin_encounter_if_needed()
    unsafe = replace(
        treasure_observation(progress=0.99),
        containment_margin_pixels=-1,
    )

    assert engine._choose_treasure_target(unsafe, 100, 1.0) is None
    assert engine._encounter is not None
    assert engine._encounter.treasure_attempts == 0


def test_treasure_attempts_are_capped_at_one_retry() -> None:
    engine = make_engine(FakeVision())
    engine._begin_encounter_if_needed()

    engine._start_treasure_attempt(1.0)
    engine._stop_treasure_attempt(1.1)
    engine._start_treasure_attempt(2.0)
    engine._stop_treasure_attempt(2.1)
    engine._start_treasure_attempt(3.0)

    assert engine._encounter is not None
    assert engine._encounter.treasure_attempts == 2
    assert not engine._treasure_attempt_active


def test_loot_transfer_clicks_each_source_once_then_verifies_menu_closed() -> None:
    vision = FakeVision()
    observations = iter(
        (
            TreasureLootObservation(True, ((100, 100),), ((100, 100),), 0.95),
            TreasureLootObservation(True, ((100, 100),), (), 0.95),
            TreasureLootObservation(True, ((100, 100),), (), 0.95),
            TreasureLootObservation(False),
        )
    )
    vision.observe_treasure_loot = lambda: next(observations)  # type: ignore[method-assign]
    engine = make_engine(vision)
    engine._begin_encounter_if_needed()
    assert engine._encounter is not None
    engine._encounter.treasure_collected = True
    hand = FakeInput()

    assert engine._loot_treasure(hand)
    assert hand.operations == ["click:100,100", "key:27"]
    assert engine._encounter.treasure_looted


def test_detected_loot_menu_confirms_previously_unverified_treasure() -> None:
    vision = FakeVision()
    observations = iter(
        (
            TreasureLootObservation(True, ((100, 100),), ((100, 100),), 0.95),
            TreasureLootObservation(True, ((100, 100),), (), 0.95),
            TreasureLootObservation(True, ((100, 100),), (), 0.95),
            TreasureLootObservation(False),
        )
    )
    vision.observe_treasure_loot = lambda: next(observations)  # type: ignore[method-assign]
    engine = make_engine(vision)
    engine._begin_encounter_if_needed()
    assert engine._encounter is not None
    engine._encounter.treasure_attempts = 1
    engine._encounter.treasure_status = TreasureStatus.SEEN
    engine._treasure_status = TreasureStatus.SEEN

    assert engine._loot_treasure(FakeInput(), required=False)
    assert engine._encounter.treasure_collected
    assert engine._encounter.treasure_looted


def test_empty_loot_menu_uses_confirmed_ok_when_escape_does_not_close() -> None:
    vision = FakeVision()
    empty = TreasureLootObservation(
        True,
        ((100, 100),),
        (),
        0.95,
        36,
        4,
        (500, 450),
    )
    observations = iter((empty, empty, empty, TreasureLootObservation(False)))
    vision.observe_treasure_loot = lambda: next(observations)  # type: ignore[method-assign]
    engine = make_engine(vision)
    hand = FakeInput()

    assert engine._loot_treasure(hand)
    assert hand.operations == ["key:27", "click:500,450"]


def test_active_treasure_attempt_latches_brief_alternating_dropouts() -> None:
    engine = make_engine(FakeVision())
    engine._begin_encounter_if_needed()
    visible = treasure_observation(progress=0.99)
    missing = replace(
        visible,
        treasure_center_y_pixels=None,
        treasure_top_y_pixels=None,
        treasure_bottom_y_pixels=None,
        treasure_confidence=0.0,
    )

    assert engine._choose_treasure_target(visible, 100, 1.000) == 200
    assert engine._choose_treasure_target(missing, 100, 1.017) == 200
    assert engine._choose_treasure_target(visible, 100, 1.034) == 200
    assert engine._choose_treasure_target(missing, 100, 1.251) is None
    assert not engine._treasure_attempt_active


def test_chest_collection_requires_continuous_disappearance_after_contact() -> None:
    engine = make_engine(FakeVision())
    engine._begin_encounter_if_needed()
    visible = treasure_observation(progress=0.99, fish_center=210, bar_center=210)
    missing = replace(
        visible,
        treasure_center_y_pixels=None,
        treasure_top_y_pixels=None,
        treasure_bottom_y_pixels=None,
        treasure_confidence=0.0,
    )
    engine._choose_treasure_target(visible, 100, 1.00)
    for index in range(1, 25):
        engine._choose_treasure_target(visible, 100, 1.00 + index * 0.02)
    engine._choose_treasure_target(missing, 100, 1.50)
    engine._choose_treasure_target(visible, 100, 1.58)
    engine._choose_treasure_target(missing, 100, 1.60)
    assert engine._treasure_status is TreasureStatus.TARGETING

    engine._choose_treasure_target(missing, 100, 1.76)
    assert engine._treasure_status is TreasureStatus.COLLECTED


def test_full_inventory_leaves_untransferred_loot_menu_open() -> None:
    vision = FakeVision()
    observations = iter(
        (
            TreasureLootObservation(
                True,
                ((100, 100),),
                ((100, 100),),
                0.95,
                inventory_slot_count=36,
                occupied_inventory_slot_count=36,
            ),
            # A full backpack may temporarily leave the item on the cursor.
            TreasureLootObservation(True, ((100, 100),), (), 0.95, 36, 36),
            TreasureLootObservation(True, ((100, 100),), (), 0.95, 36, 36),
            # Escape returns that unaccepted item to the source slot.
            TreasureLootObservation(
                True,
                ((100, 100),),
                ((100, 100),),
                0.95,
                inventory_slot_count=36,
                occupied_inventory_slot_count=36,
            ),
        )
    )
    vision.observe_treasure_loot = lambda: next(observations)  # type: ignore[method-assign]
    engine = make_engine(vision)
    hand = FakeInput()

    assert not engine._loot_treasure(hand)
    assert hand.operations == ["click:100,100", "key:27"]


def test_energy_consensus_refuels_and_reselects_the_rod() -> None:
    vision = FakeVision()
    energy = iter(
        (
            EnergyObservation(True, 0.30, 0.95),
            EnergyObservation(True, 0.30, 0.95),
            EnergyObservation(True, 0.30, 0.95),
            EnergyObservation(True, 0.80, 0.95),
        )
    )
    vision.observe_energy = lambda: next(energy)  # type: ignore[method-assign]
    vision.observe_food_prompt = lambda: FoodPromptObservation(  # type: ignore[method-assign]
        True, 0.95, (300, 200, 900, 600), (500, 500)
    )
    engine = make_engine(vision)
    hand = FakeInput()

    assert engine._ensure_energy(hand) is None
    assert engine._energy_ratio == 0.80
    assert hand.operations == [
        "idle",
        "key:50",
        "right-click:640,420",
        "click:500,500",
        "key:49",
    ]


def test_three_unreadable_energy_frames_stop_without_input() -> None:
    vision = FakeVision()
    vision.observe_energy = lambda: EnergyObservation(False)  # type: ignore[method-assign]
    engine = make_engine(vision)
    hand = FakeInput()

    assert engine._ensure_energy(hand) is EngineResult.ENERGY_UNREADABLE
    assert hand.operations == []


def test_startup_recovers_an_open_item_grab_menu_before_fishing() -> None:
    vision = FakeVision()
    observations = iter(
        (
            TreasureLootObservation(
                True,
                ((200, 150),),
                ((200, 150),),
                1.0,
                36,
                2,
            ),
            TreasureLootObservation(
                True,
                ((200, 150),),
                ((200, 150),),
                1.0,
                36,
                2,
            ),
            TreasureLootObservation(True, ((200, 150),), (), 1.0, 36, 3),
            TreasureLootObservation(True, ((200, 150),), (), 1.0, 36, 3),
            TreasureLootObservation(False),
        )
    )
    vision.observe_treasure_loot = lambda: next(observations)  # type: ignore[method-assign]
    clock = AdvancingClock()

    def slow_clock() -> float:
        clock.value += 0.01
        return clock.value

    engine = make_engine(vision, slow_clock)
    hand = FakeInput()

    assert engine._recover_open_loot_menu(hand)
    assert "click:200,150" in hand.operations
    assert "key:27" in hand.operations


def test_no_auto_eat_skips_energy_capture() -> None:
    vision = FakeVision()
    called = False

    def observe_energy() -> EnergyObservation:
        nonlocal called
        called = True
        return EnergyObservation(False)

    vision.observe_energy = observe_energy  # type: ignore[method-assign]
    engine = AutomationEngine(
        ReelPilotSettings(
            automation_mode=AutomationMode.MINIGAME_ONLY,
            auto_eat=False,
        ),
        vision,
        FakeDashboard(),
        stop_requested=lambda: False,
        pause_requested=lambda: False,
    )

    assert engine._ensure_energy(FakeInput()) is None
    assert not called


def test_default_hotbar_slot_mapping_covers_all_twelve_slots() -> None:
    assert [AutomationEngine._hotbar_virtual_key(slot) for slot in range(1, 13)] == [
        *map(ord, "1234567890"),
        0xBD,
        0xBB,
    ]
