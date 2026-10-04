param(
    [string]$Go = "$env:LOCALAPPDATA\Temp\tattler-go-20261004105106\go\bin\go.exe",
    [string]$Packager = "C:\Users\patri\source\spk-packager-sol-final-20261004",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Output) {
    $Output = Join-Path $Root "dist\OCD-armada38x-0.1.0-0001.spk"
}

$Payload = Join-Path $Root "package-source\payload\bin"
New-Item -ItemType Directory -Force -Path $Payload | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Output) | Out-Null

$env:CGO_ENABLED = "0"
$env:GOOS = "linux"
$env:GOARCH = "arm"
$env:GOARM = "7"
& $Go build -trimpath -buildvcs=false -ldflags="-s -w -buildid= -X github.com/thebrazenbeard/ocd/internal/buildinfo.Version=0.1.0-0001" -o (Join-Path $Payload "ocd") "$Root\cmd\ocd"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:PYTHONPATH = Join-Path $Packager "src"
$manifest = Join-Path $Root "package-source\spk-packager.toml"
python -m spk_packager.cli lint $manifest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m spk_packager.cli build $manifest --output $Output
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m spk_packager.cli verify $Output
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Get-FileHash (Join-Path $Payload "ocd") -Algorithm SHA256
Get-FileHash $Output -Algorithm SHA256
