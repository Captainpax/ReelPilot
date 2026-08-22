# Contributing

Thank you for helping improve ReelPilot.

1. Keep all production and test code independently authored.
2. Do not commit Stardew Valley assets, screenshots, XNB data, or private recordings.
3. Use synthetic fixtures for public detector tests.
4. Preserve F8 mouse-up behavior and the 20 ms control-loop isolation.
5. Add unit tests for every state, detector, storage, or protocol change.

Before opening a pull request, run:

```powershell
python -m pytest -q
python -m ruff check src tests
python -m mypy src\reelpilot
cargo test --manifest-path native\reelpilot-input\Cargo.toml
cargo clippy --manifest-path native\reelpilot-input\Cargo.toml -- -D warnings
```

Use descriptive PascalCase class names, verb-based snake-case functions, and unit-bearing
variable names. Keep platform APIs behind typed adapters and keep disk/UI work outside the
control loop.
