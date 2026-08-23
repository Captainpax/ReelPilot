"""GUI launcher that opens the packaged ReelPilot console in a dedicated terminal."""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from pathlib import Path


def quote_powershell(value: str) -> str:
    """Quote one literal PowerShell argument without evaluation."""
    return "'" + value.replace("'", "''") + "'"


def encoded_launch_command(core_path: Path, arguments: list[str]) -> str:
    """Build a UTF-16LE encoded script that safely forwards launcher arguments."""
    argument_array = ",".join(quote_powershell(value) for value in arguments)
    script = (
        f"$core={quote_powershell(str(core_path))};"
        f"$arguments=@({argument_array});"
        "& $core @arguments;"
        "$code=$LASTEXITCODE;"
        "Write-Host '';"
        "Read-Host 'Press Enter to close ReelPilot';"
        "exit $code"
    )
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def main() -> int:
    """Locate the packaged core and launch it in Windows Terminal or PowerShell."""
    root = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[1]
    )
    core = root / "ReelPilot.Console.exe"
    if not core.is_file():
        subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-Command",
                f"Write-Error {quote_powershell(f'ReelPilot core not found: {core}')}; Read-Host 'Press Enter'",
            ],
            check=False,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return 1
    encoded = encoded_launch_command(core, sys.argv[1:])
    powershell = shutil.which("powershell.exe") or "powershell.exe"
    terminal = shutil.which("wt.exe")
    command = [powershell, "-NoLogo", "-NoProfile", "-EncodedCommand", encoded]
    if terminal:
        subprocess.Popen([terminal, "-w", "new", *command])
    else:
        subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
