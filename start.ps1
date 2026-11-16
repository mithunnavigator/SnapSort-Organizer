# SnapSort Organizer for Windows: sets itself up the first time, then opens http://127.0.0.1:8765
# Safe to run again: if it's already running it just opens the page.
#   .\start.ps1           your photos
#   .\start.ps1 -Demo     a fake camera roll, to see what it looks like
#   .\start.ps1 -Setup    only install / repair, don't start
param([switch]$Demo, [switch]$Setup)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$port = if ($env:PORT) { $env:PORT } else { "8765" }
$url = "http://127.0.0.1:$port/"
if ($Demo) { $url += "?demo" }
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

function Test-Running {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/api/state" | Out-Null; return $true }
    catch { return $false }
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Find-Python {
    # The Microsoft Store "python.exe" stub in WindowsApps only opens the Store, so test each candidate for real.
    $candidates = @(@("py", "-3"), @("python"), @("python3"))
    # Also look where python.org / winget install it, in case PATH in this window predates the install.
    $dirs = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*", "$env:ProgramFiles\Python")
    foreach ($d in $dirs) {
        Get-ChildItem -Path $d -Filter python.exe -Recurse -Depth 1 -ErrorAction SilentlyContinue |
            Where-Object { $_.Directory.Name -match '^Python3\d+$' } | Sort-Object FullName -Descending |
            ForEach-Object { $candidates += ,@($_.FullName) }
    }
    foreach ($c in $candidates) {
        $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $v = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 9) {
                return ,(@($exe) + $rest)
            }
        } catch {}
    }
    return $null
}

if (-not $Setup -and (Test-Running)) {
    Say "SnapSort Organizer is already running." Green
    Start-Process $url
    exit 0
}

# ---------- one-time setup ----------
if ($Setup -or -not (Test-Path $venvPy)) {
    Say "Setting up SnapSort Organizer (one time only)..." Cyan
    $py = Find-Python
    if (-not $py) {
        Say "Python 3 is not installed." Yellow
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $ans = Read-Host "Install Python 3.12 from python.org with winget now? [Y/n]"
            if ($ans -eq "" -or $ans -match '^[Yy]') {
                winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
                Refresh-Path
                $py = Find-Python
            }
        }
        if (-not $py) {
            Say "Please install Python 3.9 or newer from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." Red
            Read-Host "Press Enter to close"
            exit 1
        }
    }
    $pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
    Say "Using Python: $(& $pyExe @pyArgs --version)"
    if (-not (Test-Path $venvPy)) { & $pyExe @pyArgs -m venv .venv; if ($LASTEXITCODE) { throw "Could not create .venv" } }
    & $venvPy -m pip install -q --disable-pip-version-check --upgrade pip
    & $venvPy -m pip install -q --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE) { Say "Installing the Python packages failed. Check your internet connection and run this again." Red; Read-Host "Press Enter to close"; exit 1 }

    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Say "Note: ffmpeg is not installed, so videos get grey previews (they are still sorted)." Yellow
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $ans = Read-Host "Install ffmpeg with winget now for video previews? [y/N]"
            if ($ans -match '^[Yy]') {
                winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
                Refresh-Path
            }
        }
    }
    $claudeFound = (Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path "$env:USERPROFILE\.local\bin\claude.exe")
    if (-not $claudeFound) {
        Say "Note: Claude Code was not found. Install it from https://claude.com/claude-code so Claude can label your photos." Yellow
    }
    Say "Setup done." Green
    if ($Setup) { exit 0 }
}

if ($Demo -and -not (Test-Path "dashboard\demo\mini\0.jpg")) { & $venvPy scripts\demo.py }

# make sure the server can find claude even if PATH was just updated by the installer
if (Test-Path "$env:USERPROFILE\.local\bin") { $env:Path = "$env:USERPROFILE\.local\bin;$env:Path" }
$env:PORT = $port
$env:PYTHONUTF8 = "1"

Say ""
Say "SnapSort Organizer is running at $url" Green
Say "Keep this window open while you sort. Close it (or press Ctrl+C) to stop."
Start-Job -ScriptBlock { param($u) Start-Sleep 2; Start-Process $u } -ArgumentList $url | Out-Null
& $venvPy -u scripts\server.py
