@echo off
setlocal EnableExtensions
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
if not defined GO set "GO=go"
if not defined PYTHON set "PYTHON=python"
set "MANIFEST=%ROOT%\package-source\spk-packager.toml"
set "SOURCE_URL=https://github.com/thebrazenbeard/ocd"
set "EXPECTED_GO_VERSION=go1.23.12"

for /f "tokens=3" %%V in ('findstr /b /c:"version = " "%MANIFEST%"') do set "VERSION=%%~V"
for /f %%R in ('git -C "%ROOT%" rev-parse HEAD') do set "REVISION=%%R"
for /f "tokens=3" %%G in ('"%GO%" version') do set "GO_VERSION=%%G"

if not "%GO_VERSION%"=="%EXPECTED_GO_VERSION%" (
  echo SPK builds require %EXPECTED_GO_VERSION%; got %GO_VERSION% 1>&2
  exit /b 2
)
if "%REVISION%"=="" (
  echo Unable to bind the SPK to an exact Git revision. 1>&2
  exit /b 2
)
git -C "%ROOT%" status --porcelain --untracked-files=all | findstr /r "." >nul
if not errorlevel 1 (
  echo Refusing provenance-bearing SPK build from a dirty working tree. 1>&2
  exit /b 2
)

if "%~1"=="" (
  set "OUTPUT=%ROOT%\dist\OCD-armada38x-%VERSION%.spk"
) else (
  set "OUTPUT=%~f1"
)
set "PAYLOAD=%ROOT%\package-source\payload\bin"
set "PROVENANCE=%ROOT%\package-source\payload\SOURCE_PROVENANCE.json"
if not exist "%PAYLOAD%" mkdir "%PAYLOAD%"
if not exist "%ROOT%\dist" mkdir "%ROOT%\dist"

"%PYTHON%" -c "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(json.dumps({'build_contract':'repo-contained','builder_go_version':sys.argv[5],'external_metadata_inputs':['TVmaze','TMDB','embedded-media-tags'],'logic_authority':'repository','product':'OCD','schema':'OCD_SOURCE_PROVENANCE_V1','source_repository':sys.argv[4],'source_revision':sys.argv[3],'source_revision_url':sys.argv[4]+'/commit/'+sys.argv[3],'version':sys.argv[2]},sort_keys=True,indent=2)+'\n',encoding='utf-8')" "%PROVENANCE%" "%VERSION%" "%REVISION%" "%SOURCE_URL%" "%GO_VERSION%"
if errorlevel 1 exit /b %errorlevel%

set "CGO_ENABLED=0"
set "GOOS=linux"
set "GOARCH=arm"
set "GOARM=7"
set "LDFLAGS=-s -w -buildid= -X github.com/thebrazenbeard/ocd/internal/buildinfo.Version=%VERSION% -X github.com/thebrazenbeard/ocd/internal/buildinfo.Revision=%REVISION% -X github.com/thebrazenbeard/ocd/internal/buildinfo.SourceURL=%SOURCE_URL%"
"%GO%" build -mod=vendor -trimpath -buildvcs=false -ldflags="%LDFLAGS%" -o "%PAYLOAD%\ocd" "%ROOT%\cmd\ocd"
if errorlevel 1 exit /b %errorlevel%

set "PYTHONPATH=%ROOT%\tools\spk_packager"
"%PYTHON%" -m spk_packager.cli lint "%MANIFEST%"
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" -m spk_packager.cli build "%MANIFEST%" --output "%OUTPUT%"
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" -m spk_packager.cli verify "%OUTPUT%"
if errorlevel 1 exit /b %errorlevel%

certutil -hashfile "%PAYLOAD%\ocd" SHA256
if errorlevel 1 exit /b %errorlevel%
certutil -hashfile "%OUTPUT%" SHA256
exit /b %errorlevel%
