# reelpilot-input

`reelpilot-input` is ReelPilot's small Rust learning project and fail-safe Windows input
helper. It exists to keep the timing-sensitive 20 ms mouse pulse loop independent from
Python vision, SQLite, recording, and dashboard work.

The helper is part of an educational single-player project. It is not an anti-cheat bypass,
does not read or modify game memory, and is not intended for competitive play.

## Start with the code

- [`src/main.rs`](src/main.rs) contains the versioned binary protocol, validation, Windows
  `SendInput` adapter, deadline loop, panic/EOF cleanup, and unit tests.
- [`../../src/reelpilot/input/controller.py`](../../src/reelpilot/input/controller.py) is the
  typed Python parent-process client.
- [`../../src/reelpilot/platform/windows.py`](../../src/reelpilot/platform/windows.py)
  handles window discovery, hotkeys, focus, and the second mouse-up safety layer.
- [`../../src/reelpilot/protocols.py`](../../src/reelpilot/protocols.py) defines the Python
  interfaces used by production code and fakes.
- [`../../SECURITY.md`](../../SECURITY.md) documents the input-safety policy.

## Protocol and safety model

Python starts the helper with inherited stdin/stdout handles and sends the detected Stardew
window handle during its versioned handshake. Commands set duty, press, release, idle, or
shutdown state. The helper rejects malformed/non-finite input and clamps duty to `[0, 1]`.

Mouse-up is sent on idle, shutdown, EOF, protocol failure, panic, and normal process exit.
The Python parent also releases both mouse buttons during cleanup. F7 pause and F8 emergency
stop therefore do not depend on one cleanup layer working perfectly.

## Development

From this directory:

```powershell
cargo test
cargo clippy -- -D warnings
cargo build --release
```

The full Windows package is assembled from the repository root by
[`scripts/build.ps1`](../../scripts/build.ps1). Do not commit the compiled executable or
Cargo `target` directory; releases are built from source by
[`release.yml`](../../.github/workflows/release.yml).
