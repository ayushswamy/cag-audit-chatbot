# Build app-src.zip for the offline server bundle: the repo source + the
# ingest cache (so the server loads the corpus without re-parsing PDFs or
# calling the LLM). Excludes venv, models, data, caches, git.
$ErrorActionPreference = "Stop"
$root = "d:\CAG Chatbot"
$bundle = "D:\cag-offline-bundle"
$stage = Join-Path $env:TEMP "cag-src-stage"

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
New-Item -ItemType Directory -Force $bundle | Out-Null

# top-level files
$files = @("requirements.txt", ".env.example", "docker-compose.yml", "Dockerfile",
           "README.md", "CLAUDE.md", "network-whitelist-request.md",
           "postman_collection.json", ".gitignore")
foreach ($f in $files) {
    if (Test-Path (Join-Path $root $f)) { Copy-Item (Join-Path $root $f) $stage }
}

# source dirs
$dirs = @("app", "ingest", "scripts", "eval", "tests", "docs")
foreach ($d in $dirs) {
    Copy-Item (Join-Path $root $d) (Join-Path $stage $d) -Recurse
}

# ingest cache ONLY (parsed + reconstructed records) — not logs
New-Item -ItemType Directory -Force (Join-Path $stage "ingest_out\cache") | Out-Null
Copy-Item (Join-Path $root "ingest_out\cache\*") (Join-Path $stage "ingest_out\cache") -Recurse

# strip __pycache__
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

$zip = Join-Path $bundle "app-src.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal
Remove-Item $stage -Recurse -Force
"built $zip  ({0:N1} MB)" -f ((Get-Item $zip).Length / 1MB)
