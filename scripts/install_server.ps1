<#
  install_server.ps1 - one-shot offline server setup for the CAG Audit-Para API.

  Run this ON THE SERVER (10.0.8.107), from the transferred bundle, in an
  ELEVATED PowerShell (Run as Administrator - needed for the firewall rule).

  Prerequisites (install these first, from the bundle's installers\ folder):
    - Python 3.12 (x64, "Add to PATH")   installers\python-3.12.2-amd64.exe
    - PostgreSQL 16                       installers\postgresql-16.4-1-windows-x64.exe
    - Ollama                              installers\OllamaSetup.exe
  (Or pass -InstallPrereqs to install them silently - see the top of the file.)

  What this script does (idempotent where possible):
    1. verify Python 3.12 / psql / ollama
    2. copy the qwen3:8b model blobs into Ollama's store
    3. unzip the app to -AppDir, copy models + data, place .env
    4. create the venv and pip-install OFFLINE from the wheelhouse
    5. create the Postgres role + database
    6. load the corpus (from the bundled ingest cache - no LLM, no PDF re-parse)
    7. open the Windows Firewall for TCP 8080
    8. print (or run) the start command

  Usage (from the bundle folder):
    powershell -ExecutionPolicy Bypass -File .\install_server.ps1 `
        -BundleDir "C:\cag-offline-bundle" -AppDir "C:\CAG Chatbot" `
        -PgSuperPassword "<the postgres superuser password you set at install>" `
        -StartServer
#>
[CmdletBinding()]
param(
    [string]$BundleDir = "",
    [string]$AppDir = "C:\CAG Chatbot",
    [string]$PgSuperPassword = "postgres",
    [string]$DbUser = "audit",
    [string]$DbPassword = "audit",
    [string]$DbName = "audit",
    [switch]$InstallPrereqs,
    [switch]$StartServer
)

$ErrorActionPreference = "Stop"
# Resolve the bundle folder robustly (the -File launcher can leave
# $MyInvocation.MyCommand.Path null, so prefer $PSScriptRoot / the CWD).
if (-not $BundleDir) {
    $BundleDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
}
function Step($n, $msg) { Write-Host "`n=== [$n] $msg ===" -ForegroundColor Cyan }
function Ok($m)  { Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m){ Write-Host "    !!  $m" -ForegroundColor Yellow }

if (-not (Test-Path $BundleDir)) { throw "Bundle folder not found: $BundleDir" }

# --------------------------------------------------------------------------- #
if ($InstallPrereqs) {
    Step 0 "Silently installing prerequisites"
    $py = Join-Path $BundleDir "installers\python-3.12.2-amd64.exe"
    $pg = Join-Path $BundleDir "installers\postgresql-16.4-1-windows-x64.exe"
    $ol = Join-Path $BundleDir "installers\OllamaSetup.exe"
    if (Test-Path $py) { Start-Process $py -Wait -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1","Include_test=0"; Ok "Python" }
    if (Test-Path $pg) { Start-Process $pg -Wait -ArgumentList "--mode","unattended","--superpassword",$PgSuperPassword,"--serverport","5432"; Ok "PostgreSQL" }
    if (Test-Path $ol) { Start-Process $ol -Wait -ArgumentList "/VERYSILENT"; Ok "Ollama" }
    Warn "Open a NEW PowerShell so PATH updates take effect, then re-run WITHOUT -InstallPrereqs."
    return
}

# --------------------------------------------------------------------------- #
Step 1 "Verifying prerequisites"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "python not on PATH. Install Python 3.12 (Add to PATH) first." }
$pyver = & python --version
if ($pyver -notmatch "3\.12") { throw "Need Python 3.12, found: $pyver" }
Ok "$pyver at $python"

$psql = (Get-Command psql -ErrorAction SilentlyContinue).Source
if (-not $psql) {
    $cand = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cand) { $psql = $cand.FullName }
}
if (-not $psql) { throw "psql not found. Install PostgreSQL 16 first." }
Ok "psql at $psql"

$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) { throw "ollama not on PATH. Install Ollama first." }
Ok "ollama at $ollama"

# --------------------------------------------------------------------------- #
Step 2 "Loading the qwen3-8b model into Ollama"
$ollamaStore = Join-Path $env:USERPROFILE ".ollama\models"
New-Item -ItemType Directory -Force $ollamaStore | Out-Null
robocopy (Join-Path $BundleDir "ollama-models") $ollamaStore /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy of ollama-models failed ($LASTEXITCODE)" }
$models = & ollama list
if ($models -match "qwen3:8b") { Ok "qwen3:8b present" } else { Warn "qwen3:8b not listed yet; check 'ollama list'." }

# --------------------------------------------------------------------------- #
Step 3 "Deploying app + models + data + .env"
New-Item -ItemType Directory -Force $AppDir | Out-Null
Expand-Archive -Path (Join-Path $BundleDir "app-src.zip") -DestinationPath $AppDir -Force
robocopy (Join-Path $BundleDir "models") (Join-Path $AppDir "models") /E /NFL /NDL /NJH /NJS /NP | Out-Null
robocopy (Join-Path $BundleDir "data")   (Join-Path $AppDir "data")   /E /NFL /NDL /NJH /NJS /NP | Out-Null
$envPath = Join-Path $AppDir ".env"
if (-not (Test-Path $envPath)) { Copy-Item (Join-Path $BundleDir ".env.server") $envPath }
Ok "app at $AppDir (edit .env: set API_KEYS and CORS_ORIGINS)"

# --------------------------------------------------------------------------- #
Step 4 "Creating venv + OFFLINE pip install"
$venvPy = Join-Path $AppDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { & python -m venv (Join-Path $AppDir ".venv") }
& $venvPy -m pip install --no-index --find-links (Join-Path $BundleDir "wheelhouse") -r (Join-Path $BundleDir "requirements-lock.txt")
if ($LASTEXITCODE -ne 0) { throw "offline pip install failed" }
Ok "dependencies installed from wheelhouse"

# --------------------------------------------------------------------------- #
Step 5 "Creating Postgres role + database"
$env:PGPASSWORD = $PgSuperPassword
$roleSql = "DO `$`$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$DbUser') THEN CREATE ROLE $DbUser LOGIN PASSWORD '$DbPassword'; END IF; END `$`$;"
& $psql -U postgres -h localhost -v ON_ERROR_STOP=1 -c $roleSql
$dbExists = (& $psql -U postgres -h localhost -tAc "SELECT 1 FROM pg_database WHERE datname='$DbName'")
if ($dbExists -ne "1") { & $psql -U postgres -h localhost -c "CREATE DATABASE $DbName OWNER $DbUser" }
Remove-Item Env:PGPASSWORD
Ok "role '$DbUser' + database '$DbName' ready"

# --------------------------------------------------------------------------- #
Step 6 "Loading the corpus (from bundled cache - no LLM, no PDF re-parse)"
Push-Location $AppDir
$env:PYTHONUTF8 = "1"
& $venvPy -m ingest --store --embed --recreate
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "ingest --store --embed failed" }
Pop-Location
Ok "corpus loaded (50 paras to Postgres, ~180 chunks to embedded Qdrant)"

# --------------------------------------------------------------------------- #
Step 7 "Opening Windows Firewall for TCP 8080"
try {
    if (-not (Get-NetFirewallRule -DisplayName "CAG Chatbot API 8080" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "CAG Chatbot API 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow | Out-Null
    }
    Ok "inbound TCP 8080 allowed"
} catch { Warn "Could not add firewall rule (run as Administrator). Add it manually for port 8080." }

# --------------------------------------------------------------------------- #
Step 8 "Start the API"
$startCmd = "cd `"$AppDir`"; `$env:OLLAMA_KEEP_ALIVE='-1'; .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080"
if ($StartServer) {
    Ok "starting uvicorn (Ctrl+C to stop)"
    Push-Location $AppDir
    $env:OLLAMA_KEEP_ALIVE = "-1"
    & $venvPy -m uvicorn app.main:app --host 0.0.0.0 --port 8080
    Pop-Location
} else {
    Write-Host "`nDONE. Start the server with:`n  $startCmd" -ForegroundColor Green
    Write-Host "Then verify:  curl http://localhost:8080/api/v1/health" -ForegroundColor Green
}
