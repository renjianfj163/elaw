$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $root "dist\hospital-eia-windows"
$zip = Join-Path $root "dist\hospital-eia-windows.zip"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
if (Test-Path $zip) { Remove-Item $zip -Force }
New-Item -ItemType Directory -Path $stage | Out-Null
robocopy $root $stage /E /NFL /NDL /NJH /NJS /XD .git .venv node_modules dist data __pycache__ .pytest_cache .next /XF .env
if ($LASTEXITCODE -ge 8) { throw "robocopy failed: $LASTEXITCODE" }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Write-Output $zip
