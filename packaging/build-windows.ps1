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

robocopy $root $stage /E /NFL /NDL /NJH /NJS /XD .git .venv venv node_modules dist data __pycache__ .pytest_cache .next python libreoffice /XF .env *.db
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

$loVersion = "26.8.1"
$loMsiName = "LibreOffice_${loVersion}_Win_x86-64.msi"
$loUrl = "https://download.documentfoundation.org/libreoffice/stable/$loVersion/win/x86_64/$loMsiName"
$loSha256 = "C6298B10CFA1748BCBEDAD94C62CE771E240E5021BFD6D7A967CD571B04B5312"
$loMsi = Join-Path $cache $loMsiName
$loExtract = Join-Path $cache "libreoffice-$loVersion"

if (Test-Path -LiteralPath $loMsi) {
  $loActual = (Get-FileHash -LiteralPath $loMsi -Algorithm SHA256).Hash
  if ($loActual -ne $loSha256) { Remove-Item -LiteralPath $loMsi -Force }
}
if (-not (Test-Path -LiteralPath $loMsi)) {
  & curl.exe -fL --retry 3 --retry-delay 2 -o $loMsi $loUrl
  if ($LASTEXITCODE -ne 0) { throw "libreoffice download failed: $LASTEXITCODE" }
}
$loActual = (Get-FileHash -LiteralPath $loMsi -Algorithm SHA256).Hash
if ($loActual -ne $loSha256) { throw "libreoffice msi hash mismatch: $loActual" }

$found = $null
if (Test-Path -LiteralPath $loExtract) {
  $found = Get-ChildItem -LiteralPath $loExtract -Filter soffice.com -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Directory.Name -eq "program" } |
    Select-Object -First 1
}
if (-not $found) {
  if (Test-Path -LiteralPath $loExtract) { Remove-Item -LiteralPath $loExtract -Recurse -Force }
  New-Item -ItemType Directory -Path $loExtract | Out-Null
  $proc = Start-Process -FilePath msiexec.exe -ArgumentList @(
    "/a", $loMsi, "/qn", "TARGETDIR=$loExtract"
  ) -Wait -PassThru
  if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) { throw "libreoffice extract failed: $($proc.ExitCode)" }
  $found = Get-ChildItem -LiteralPath $loExtract -Filter soffice.com -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Directory.Name -eq "program" } |
    Select-Object -First 1
}
if (-not $found) { throw "LibreOffice 解压后没有 program\soffice.com" }

$loStage = Join-Path $stage "libreoffice"
if (Test-Path $loStage) { Remove-Item $loStage -Recurse -Force }
robocopy $found.Directory.Parent.FullName $loStage /E /NFL /NDL /NJH /NJS /XF *.msi
if ($LASTEXITCODE -ge 8) { throw "robocopy libreoffice failed: $LASTEXITCODE" }
$stagedSoffice = Join-Path $loStage "program\soffice.com"
if (-not (Test-Path -LiteralPath $stagedSoffice)) { throw "package is missing libreoffice\program\soffice.com" }

Get-ChildItem (Join-Path $stage "backend") -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Get-ChildItem $stage -Recurse -Filter *.db | Remove-Item -Force

Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Write-Output $zip
