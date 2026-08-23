[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$BuildDirectory = Join-Path $ProjectRoot 'build'
$DistDirectory = Join-Path $ProjectRoot 'dist'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Virtual environment not found. Run: py -3.14 -m venv .venv"
}

$PackageVersion = (& $Python -c 'from reelpilot import __version__; print(__version__)').Trim()
$ReleaseVersion = $PackageVersion -replace 'b(\d+)$', '-beta.$1'
if ($ReleaseVersion -notmatch '^\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?$') {
    throw "Cannot derive a release filename from package version: $PackageVersion"
}

foreach ($Target in @($BuildDirectory, $DistDirectory)) {
    $Parent = Split-Path -Parent $Target
    if ((Resolve-Path -LiteralPath $Parent).Path -ne $ProjectRoot) {
        throw "Refusing to clean unexpected path: $Target"
    }
    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
}

Push-Location (Join-Path $ProjectRoot 'native\reelpilot-input')
try {
    cargo test
    cargo clippy -- -D warnings
    cargo build --release
} finally {
    Pop-Location
}

Copy-Item -LiteralPath (
    Join-Path $ProjectRoot 'native\reelpilot-input\target\release\reelpilot-input.exe'
) -Destination (Join-Path $ProjectRoot 'native\reelpilot-input.exe') -Force

& $Python -m pytest -q
& $Python -m ruff check src tests scripts
& $Python -m mypy src\reelpilot
& $Python -m PyInstaller --noconfirm --clean packaging\reelpilot-console.spec
& $Python -m PyInstaller --noconfirm --clean packaging\reelpilot-launcher.spec

$ApplicationDirectory = Join-Path $DistDirectory 'ReelPilot'
New-Item -ItemType Directory -Path $ApplicationDirectory | Out-Null
Get-ChildItem -LiteralPath (Join-Path $DistDirectory 'ReelPilot.Console') |
    Copy-Item -Destination $ApplicationDirectory -Recurse -Force
Copy-Item -LiteralPath (Join-Path $DistDirectory 'ReelPilot.Launcher.exe') -Destination (Join-Path $ApplicationDirectory 'ReelPilot.exe') -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'README.md') -Destination $ApplicationDirectory
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'LICENSE') -Destination $ApplicationDirectory
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'PROVENANCE.md') -Destination $ApplicationDirectory

& (Join-Path $ApplicationDirectory 'ReelPilot.Console.exe') --version

$Archive = Join-Path $DistDirectory "ReelPilot-v$ReleaseVersion-windows-x64.zip"
Compress-Archive -LiteralPath $ApplicationDirectory -DestinationPath $Archive -CompressionLevel Optimal
$Hash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
"$Hash  $(Split-Path -Leaf $Archive)" | Set-Content -LiteralPath (Join-Path $DistDirectory 'SHA256SUMS.txt') -Encoding utf8
Write-Host "Built $Archive"
