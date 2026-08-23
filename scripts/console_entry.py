"""PyInstaller console entry point kept separate from the public package API."""

from reelpilot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
