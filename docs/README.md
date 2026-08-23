# ReelPilot documentation

This directory explains how ReelPilot is designed, measured, and kept safe. ReelPilot is
an educational single-player project for learning Python, Rust, OpenCV screen vision,
automation, real-time control, SQLite, concurrency, packaging, and QA. It is not intended
for competitive play, leaderboards, anti-cheat bypasses, or gaining an advantage over
other players.

## Quick navigation

- [Project overview, setup, and controls](../README.md)
- [Architecture and runtime boundaries](ARCHITECTURE.md)
- [Statistics, rarity, and value estimates](STATISTICS.md)
- [Sanitized automated and live QA evidence](QA_REPORT.md)
- [Clean-room-style provenance](../PROVENANCE.md)
- [Contribution rules](../CONTRIBUTING.md)
- [Input-safety reporting](../SECURITY.md)
- [Native Rust helper](../native/reelpilot-input/README.md)

## Learning paths

| Topic | Read | Explore the code |
| --- | --- | --- |
| Application composition | [Architecture](ARCHITECTURE.md) | [`app.py`](../src/reelpilot/app.py), [`protocols.py`](../src/reelpilot/protocols.py) |
| Screen vision | [Architecture: runtime flow](ARCHITECTURE.md#runtime-flow) | [`vision/`](../src/reelpilot/vision/), [`test_vision.py`](../tests/test_vision.py) |
| Feedback control | [Architecture: runtime flow](ARCHITECTURE.md#runtime-flow) | [`controller.py`](../src/reelpilot/control/controller.py), [`motion.py`](../src/reelpilot/control/motion.py) |
| Safe automation | [Architecture: invariants](ARCHITECTURE.md#important-invariants) | [`engine.py`](../src/reelpilot/automation/engine.py), [`test_engine.py`](../tests/test_engine.py) |
| Rust and Windows input | [Native helper](../native/reelpilot-input/README.md) | [`main.rs`](../native/reelpilot-input/src/main.rs), [`input/controller.py`](../src/reelpilot/input/controller.py) |
| SQLite and analytics | [Statistics](STATISTICS.md) | [`repository.py`](../src/reelpilot/stats/repository.py), [`service.py`](../src/reelpilot/stats/service.py) |
| Async telemetry | [Architecture: runtime flow](ARCHITECTURE.md#runtime-flow) | [`recorder.py`](../src/reelpilot/recording/recorder.py), [`replay.py`](../src/reelpilot/recording/replay.py) |
| Verification | [QA report](QA_REPORT.md) | [`tests/`](../tests/), [`build.ps1`](../scripts/build.ps1), [CI workflow](../.github/workflows/ci.yml) |

## Documentation screenshots

The files under [`screenshots/`](screenshots/) are terminal-only renders made from
sanitized demonstration data. They contain no Stardew Valley art or private gameplay
captures. The generator is [`scripts/generate_screenshots.py`](../scripts/generate_screenshots.py).
