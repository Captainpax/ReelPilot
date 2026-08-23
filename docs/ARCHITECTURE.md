# ReelPilot Architecture

ReelPilot separates timing-sensitive automation from capture, storage, recording, and
presentation. The engine depends on typed protocols, so tests can replay observations
without locating Stardew, launching the native helper, or moving the mouse.

## Runtime flow

1. `ReelPilotApplication` locates Stardew and opens SQLite, but remains idle until F6 or
   F7. F5 can query history without constructing input or capture services.
2. `VisionPipeline` owns one persistent MSS capture session. It acquires up to four cast
   candidates before tracking a fixed meter ROI, captures only the playable bite region,
   uses full frames for result, energy, confirmation-dialog, and treasure-loot analysis,
   and switches to a 138×471 fishing-UI ROI during control.
3. `AutomationEngine` advances explicit states and checks F8/F7 during every interruptible
   wait. F5 is accepted only while READY or PAUSED.
4. `FishingController` consumes reliable pixel observations and returns normal Python
   numeric values. OpenCV and NumPy stay inside `vision`.
5. `InputController` sends a compact protocol to `reelpilot-input.exe`. Both layers force
   mouse-up during idle and cleanup.
6. `SQLiteStatsRepository` and `SessionRecorder` own background writers so the controller
   never waits for disk or terminal rendering.

## Important invariants

- F8 and cleanup always release input before capture, recording, or SQLite are closed.
- The control loop never queries SQLite, encodes PNGs, or renders Rich output.
- Catch cards are latched once per encounter. Ambiguous OCR becomes `unknown`; no name or
  price is invented.
- Historical reads that require current data cross an ordered SQLite writer barrier.
- Runtime observations, decisions, settings, and dashboard data are immutable slotted
  dataclasses. Only the active encounter and controller filters retain mutable state.
- Perfect-first control uses predicted fish/bar edge margins. A reliable containment
  break permanently selects recovery control for that encounter; OCR alone confirms the
  in-game Perfect bonus.
- A missing cast-meter lock uses either an explicit fixed duration or the bounded median
  of the last five OCR-verified MAX holds. OCR failure never causes a blind recast.
- Treasure is a subordinate control target. The engine first attempts a bar position that
  contains both fish and chest, spends catch progress only at or above 98%, retains an
  active stationary chest for 200 ms across detector dropouts, and returns to fish recovery
  at the 72% floor or on darting/vision danger. A chest is collected only after 450 ms of
  reliable overlap and 150 ms of continuous disappearance.
- Every resolved result is inspected for an item-grab menu. Each occupied source slot is
  clicked at most once, including with a full grid so matching stacks can merge. ReelPilot
  presses Escape only after two empty-source scans, then rescans to prove the menu closed.
  A returned or unchanged source item leaves the menu open and stops automation.
- Energy and food actions occur only at safe encounter boundaries. Two of three readings
  below 33% enter `REFUELING`; three consecutive unreadable readings stop. Eating requires
  a located English `Yes` button and a verified energy gain. Four-item and 15-second bounds
  prevent an accidental unbounded consumption loop.
- Direct input cleanup releases left/right mouse buttons plus Shift, Ctrl, and Alt. F8 is
  checked around slot selection, direct clicks, OCR capture, and every 10 ms refuel wait.

## Safe examples

Create validated settings without connecting to the game:

```python
from reelpilot.domain import AutomationMode, ReelPilotSettings

settings = ReelPilotSettings(
    automation_mode=AutomationMode.HOOK_ONLY,
    rod_slot=1,
    food_slot=2,
)
settings.validate()
```

Exercise the motion estimator offline:

```python
from reelpilot.control.motion import MotionEstimator

estimator = MotionEstimator()
estimator.update(100.0, 0.00, 1.0, 96)
estimate = estimator.update(104.0, 0.02, 1.0, 96)
assert estimate.velocity_pixels_per_second > 0
```

See [STATISTICS.md](STATISTICS.md) for schema, rarity, price, and F5 semantics.
