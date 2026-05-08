$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "Checking Python..."
$PythonExe = ((& cmd.exe /c "where python.exe" 2>$null) | Select-Object -First 1)
if (-not $PythonExe) {
  $PythonExe = @(
    "C:\Python314\python.exe",
    "$env:USERPROFILE\miniconda3\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
  ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $PythonExe) {
  throw "Python was not found on PATH."
}

Write-Host "Checking Node.js..."
$NodeExe = ((& cmd.exe /c "where node.exe" 2>$null) | Select-Object -First 1)
$NpmExe = ((& cmd.exe /c "where npm.cmd" 2>$null) | Select-Object -First 1)
if (-not $NodeExe) {
  $NodeExe = @("C:\Program Files\nodejs\node.exe", "$env:ProgramFiles\nodejs\node.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $NpmExe) {
  $NpmExe = @("C:\Program Files\nodejs\npm.cmd", "$env:ProgramFiles\nodejs\npm.cmd") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $NodeExe -or -not $NpmExe) {
  throw "Node.js was not found on PATH."
}

if (-not (Test-Path ".venv")) {
  Write-Host "Creating .venv..."
  & $PythonExe -m venv .venv
}

Write-Host "Installing Python dependencies..."
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt

Write-Host "Installing frontend dependencies..."
Set-Location ".\auto_research\web\client"
& $NpmExe install

Set-Location $Root
Write-Host "Setup complete."
