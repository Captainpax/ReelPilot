# ReelPilot QA Report

Date: 2026-08-22
Target: ReelPilot `0.1.0-beta.2`, Stardew Valley 1.6.15, Windows 11,
minimum window size, 75% game/UI scale

This report contains sanitized results only. It does not include game captures,
private recordings, save data, or personally identifying paths.

## Automated verification

The release build runs these checks from `scripts/build.ps1`:

```powershell
python -m pytest -q
python -m ruff check src tests
python -m mypy src
cargo test --manifest-path native/reelpilot-input/Cargo.toml
cargo clippy --manifest-path native/reelpilot-input/Cargo.toml -- -D warnings
```

Final result:

- 119 Python tests passed.
- 5 Rust helper tests passed.
- Ruff, mypy, and Clippy passed without warnings.
- The PyInstaller console and launcher builds completed successfully.
- The packaged console passed `--version`, `--help`, and offline replay smoke tests.
- The Windows x64 ZIP and SHA-256 checksum were regenerated after the final fixes.

The tests cover automation states, safe input cleanup, cast and bite detection,
controller behavior, result latching, paused-result recognition, SQLite schema-v5
migration and aggregation, F5 history behavior, recording formats 1-7, dashboard
rendering, treasure targeting and loot transfer, energy/refueling safeguards, inventory-full
stops, Windows input timing, and documentation examples.

## Live QA workflow

Live control used the packaged `ReelPilot.Console.exe` with F6 debug recording.
Stardew began and ended each test in the Journal so the game was paused whenever
automation was not being observed.

The broad statistics run completed 21 terminal encounters. It verified F5 totals
against independent read-only SQL, active-F5 rejection, F7 pause/resume, result
deduplication, debug recording, and clean F8 shutdown. It also exposed a physical
input problem: high duty was encoded as repeated 20 ms clicks, so the green bar
could remain near the bottom even when the controller requested full lift.

The corrected helper preserves mouse-down across control cycles at near-full duty.
A second live comparison then completed three out of three real minigames:

- Catch progress reached 100% in all three encounters.
- Fish containment was 97.0-100.0% of reliable control frames.
- Feasible centered time increased from 23.0% to 56.6% overall.
- Two encounters measured 59.0% and 100.0% feasible centered time; the remaining
  fast upward-moving encounter stayed contained but lagged center.
- Duty saturation fell from 43.6% to 21.5%.
- Median control cycle was 15.46 ms; p95 was 23.62 ms.
- F8 stopped the process, released input, flushed the recording, and left no native
  helper process running.

An experimental stronger darting profile produced more edge risk and two escapes,
so it was rejected and the safer three-for-three tuning was restored. This is an
intentional evidence-based rollback, not the final configuration.

The final extended run exercised nine casts and eight hooks. It retained five fish,
recognized one non-fish item, recorded one escape and one conservative unknown result,
and shut down cleanly through F8. The control loop measured 16.94 ms median and 28.36 ms
p95. Energy remained above the refueling threshold (minimum 76.25%), so this run did not
consume food. One chest appeared; ReelPilot attempted it once, abandoned it to protect the
fish, and still completed the catch. This validates the fish-first abort path, not a live
treasure-collection success.

A follow-up hard/darting encounter exposed two separate issues. Shallow bar-edge occlusion
could falsely mark Perfect as missed, so the detector now snaps credible bar edges to the
track boundary and only treats a shallow negative margin as a break when catch progress also
regresses. A mistaken controller-coordinate experiment was rejected by a new detector/
controller invariant test. The corrected hard encounter remained physically difficult and
escaped; ReelPilot therefore does not claim the Perfect-rate acceptance target from this
sample.

The live item-grab investigation established that source stacks require an ordinary click,
not Shift-click. A coal stack transferred successfully. The automated close path now waits
between Win32 button/key down and up events, moves the cursor away before rescanning, and
only closes after two empty scans. Its final end-to-end close was verified offline and in the
packaged build, but no second live treasure menu spawned after the timing fix.

## Defects fixed during QA

- Full-duty helper pulses were interpreted as clicks instead of a held button.
- Center correction intentionally tolerated too much top-edge bias.
- Static scenery could be mistaken for a cast-meter lock.
- A missing catch card near full progress could cause a false unknown result and a
  blind dismissal click.
- Bite-timeout encounter rows could remain unfinished.
- The helper cursor target overlapped Stardew's HUD.
- A result card appearing after F7 during a live minigame could be lost before stats
  recorded it; paused mode now scans at 5 Hz without sending input.
- A fish sprite could hide the green bar at the track boundary and produce a false Perfect
  failure even while catch progress increased.
- Treasure-menu Shift-clicks were ignored by Stardew; guarded ordinary clicks are now used.
- The Windows cursor glow could be counted as an occupied inventory slot.
- Direct mouse/key down and up events in the same scheduler slice could be missed by the
  game; bounded polling intervals now separate those events.
- Controller and detector track coordinates can no longer diverge silently because their
  shared geometry is asserted in tests.

## Remaining limitations

- Exact visual centering is not always physically reachable when a fish darts toward
  a track boundary. ReelPilot prioritizes containment and avoiding destabilizing
  overshoot in those cases.
- Result OCR remains conservative. Ambiguous cards are stored as `unknown` rather
  than inventing a fish name.
- Cast-meter detection can still use the timed fallback when a reliable meter lock
  is unavailable.
- General inventory handling, movement, legendary optimization, and display scales outside
  the documented baseline remain out of scope. Fishing-treasure transfer stops safely with
  the menu left open when inventory space is unavailable.
- The safe auto-eat state machine, dialog OCR guard, energy-increase verification, four-item
  limit, and F8 cleanup pass offline tests. Live energy never fell below 33%, so food
  consumption to 75% is not claimed as live-verified in this report.
- The full-inventory detector and leave-menu-open behavior pass synthetic and engine tests;
  deliberately filling the player's live backpack was not performed.

## Perfect-first update verification

The version-5 offline suite adds multi-candidate cast acquisition, playable-region bite
geometry, adaptive verified-MAX fallback, fish/bar edge margins, Perfect safety overrides,
recovery latching, schema-v3 migration, and Perfect/MAX aggregation. Live acceptance is
reported separately after a packaged run; offline success is not presented as proof of a
Perfect catch because only Stardew's visible indicator can confirm it.

## Treasure and refueling update verification

Recording format 7 and SQLite schema 5 add fish-first treasure observations, control-target
telemetry, encounter counters, post-catch menu recognition, guarded single-click transfer,
energy observations, food counters, and inventory-full stop totals.
Synthetic UI/menu fixtures cover chest masking, shared fish/chest containment, progress
reserve aborts, transfer ordering, empty-menu completion, and the full-inventory safe stop.
A private recorded chest sequence confirms that warm chest pixels are not reused as the fish
position and that a 200 ms latch survives alternating detector gaps. Energy fixtures cover
full, threshold, low, empty, nighttime, and invalid-HUD readings. Engine tests cover repeated
eating to 75%, missing/non-food prompts, no energy increase, bounded time/item limits, rod
reselection, F7/F8 interruption, and unconditional release of both mouse buttons. These
offline results are kept distinct from the narrower live observations above.
