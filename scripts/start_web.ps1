$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
  $Python = ((& cmd.exe /c "where python.exe" 2>$null) | Select-Object -First 1)
  if (-not $Python) {
    $Python = @(
      "C:\Python314\python.exe",
      "$env:USERPROFILE\miniconda3\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
  }
  if (-not $Python) {
    throw "Python was not found. Run scripts\setup_windows.ps1 first."
  }
}
$NpmExe = ((& cmd.exe /c "where npm.cmd" 2>$null) | Select-Object -First 1)
if (-not $NpmExe) {
  $NpmExe = @("C:\Program Files\nodejs\npm.cmd", "$env:ProgramFiles\nodejs\npm.cmd") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $NpmExe) {
  throw "npm.cmd was not found. Install Node.js first."
}

Write-Host "Building frontend..."
Set-Location ".\auto_research\web\client"
& $NpmExe run build

Set-Location $Root
Write-Host "Starting TASTE at http://127.0.0.1:8765"
& $Python -m uvicorn auto_research.web.server:app --host 127.0.0.1 --port 8765
