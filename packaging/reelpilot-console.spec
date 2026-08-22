from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path.cwd()
hidden_imports = []
for package in (
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
    "winrt.windows.graphics.imaging",
    "winrt.windows.media.ocr",
    "winrt.windows.storage.streams",
):
    hidden_imports.extend(collect_submodules(package))

analysis = Analysis(
    [str(ROOT / "scripts" / "console_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[(str(ROOT / "native" / "reelpilot-input.exe"), "native")],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
python_archive = PYZ(analysis.pure)
executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="ReelPilot.Console",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="ReelPilot.Console",
)
