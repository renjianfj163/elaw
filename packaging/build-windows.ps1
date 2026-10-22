$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $root "dist\hospital-eia-windows"
$zip = Join-Path $root "dist\hospital-eia-windows.zip"
$cache = Join-Path $root "dist\cache"
$pyVersion = "3.11.9"
$pyZipName = "python-$pyVersion-embed-amd64.zip"
$pyUrl = "https://www.python.org/ftp/python/$pyVersion/$pyZipName"
$pySha256 = "009D6BF7E3B2DDCA3D784FA09F90FE54336D5B60F0E0F305C37F400BF83CFD3B"

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
if (Test-Path $zip) { Remove-Item $zip -Force }
New-Item -ItemType Directory -Path $stage | Out-Null
New-Item -ItemType Directory -Force -Path $cache | Out-Null

robocopy $root $stage /E /NFL /NDL /NJH /NJS /XD .git .venv venv node_modules dist data __pycache__ .pytest_cache .next python /XF .env *.db
if ($LASTEXITCODE -ge 8) { throw "robocopy failed: $LASTEXITCODE" }
# Keep docs/index.html only. Sample folders beside it must not ship.
Get-ChildItem (Join-Path $stage "docs") -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
if (Get-ChildItem (Join-Path $stage "docs") -Directory -ErrorAction SilentlyContinue) { throw "sample document folders were copied into the package" }
if (Test-Path (Join-Path $stage ".env")) { throw ".env was copied into the package" }

$repeal = Join-Path $root "data\repealed_map.csv"
if (-not (Test-Path $repeal)) { throw "missing data\repealed_map.csv" }
New-Item -ItemType Directory -Path (Join-Path $stage "data") | Out-Null
Copy-Item $repeal (Join-Path $stage "data\repealed_map.csv")

$pyZip = Join-Path $cache $pyZipName
if (-not (Test-Path $pyZip)) {
  Invoke-WebRequest -Uri $pyUrl -OutFile $pyZip
}
$actual = (Get-FileHash $pyZip -Algorithm SHA256).Hash
if ($actual -ne $pySha256) { throw "python embeddable hash mismatch: $actual" }

$pyDir = Join-Path $stage "python"
Expand-Archive -Path $pyZip -DestinationPath $pyDir
$python = Join-Path $pyDir "python.exe"
if (-not (Test-Path $python)) { throw "embeddable python.exe missing" }

$pth = "python311.zip`r`n.`r`nLib\site-packages`r`n..\backend`r`nimport site`r`n"
[System.IO.File]::WriteAllText((Join-Path $pyDir "python311._pth"), $pth, [System.Text.Encoding]::ASCII)

$getPip = Join-Path $cache "get-pip.py"
Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
& $python $getPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "get-pip failed: $LASTEXITCODE" }

$runtime = Join-Path $PSScriptRoot "requirements-runtime.txt"
& $python -m pip install --disable-pip-version-check --no-warn-script-location --no-cache-dir -r $runtime
if ($LASTEXITCODE -ne 0) { throw "pip install failed: $LASTEXITCODE" }

$env:DATABASE_URL = "sqlite://"
$env:DATA_DIR = Join-Path $env:TEMP "elaw-package-smoke"
& $python -c "import fastapi, uvicorn, pydantic, sqlalchemy, yaml, pint, pypdf, httpx, jieba; from rank_bm25 import BM25Okapi; import app.main; assert app.main.DISCLAIMER"
if ($LASTEXITCODE -ne 0) { throw "bundled python could not import the app" }

Get-ChildItem (Join-Path $stage "backend") -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Get-ChildItem $stage -Recurse -Filter *.db | Remove-Item -Force

Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Write-Output $zip
