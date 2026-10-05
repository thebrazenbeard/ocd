param(
    [string]$Go = "go",
    [string]$Python = "python",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Manifest = Join-Path $Root "package-source\spk-packager.toml"
$SourceURL = "https://github.com/thebrazenbeard/ocd"
$Version = (& $Python -c "import sys,tomllib; print(tomllib.load(open(sys.argv[1],'rb'))['package']['version'])" $Manifest).Trim()
$Revision = (& git -C $Root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Revision -notmatch '^[0-9a-f]{40}$') {
    throw "Unable to bind the SPK to an exact Git revision."
}
$Dirty = & git -C $Root status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0) { throw "Unable to inspect Git working-tree state." }
if ($Dirty) { throw "Refusing provenance-bearing SPK build from a dirty working tree." }

if (-not $Output) {
    $Output = Join-Path $Root "dist\OCD-armada38x-$Version.spk"
}
$Payload = Join-Path $Root "package-source\payload\bin"
$Provenance = Join-Path $Root "package-source\payload\SOURCE_PROVENANCE.json"
New-Item -ItemType Directory -Force -Path $Payload | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Output) | Out-Null

& $Python -c "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(json.dumps({'build_contract':'repo-contained','external_metadata_inputs':['TVmaze','TMDB','embedded-media-tags'],'logic_authority':'repository','product':'OCD','schema':'OCD_SOURCE_PROVENANCE_V1','source_repository':sys.argv[4],'source_revision':sys.argv[3],'source_revision_url':sys.argv[4]+'/commit/'+sys.argv[3],'version':sys.argv[2]},sort_keys=True,indent=2)+'\n',encoding='utf-8')" $Provenance $Version $Revision $SourceURL
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:CGO_ENABLED = "0"
$env:GOOS = "linux"
$env:GOARCH = "arm"
$env:GOARM = "7"
$LdFlags = "-s -w -buildid= -X github.com/thebrazenbeard/ocd/internal/buildinfo.Version=$Version -X github.com/thebrazenbeard/ocd/internal/buildinfo.Revision=$Revision -X github.com/thebrazenbeard/ocd/internal/buildinfo.SourceURL=$SourceURL"
& $Go build -mod=vendor -trimpath -buildvcs=false -ldflags=$LdFlags -o (Join-Path $Payload "ocd") "$Root\cmd\ocd"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:PYTHONPATH = Join-Path $Root "tools\spk_packager"
& $Python -m spk_packager.cli lint $Manifest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m spk_packager.cli build $Manifest --output $Output
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m spk_packager.cli verify $Output
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Get-FileHash (Join-Path $Payload "ocd") -Algorithm SHA256
Get-FileHash $Output -Algorithm SHA256
