#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GO="${GO:-go}"
PYTHON="${PYTHON:-python3}"
MANIFEST="$ROOT/package-source/spk-packager.toml"
SOURCE_URL="https://github.com/thebrazenbeard/ocd"
VERSION="$("$PYTHON" -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb"))["package"]["version"])' "$MANIFEST")"
REVISION="$(git -C "$ROOT" rev-parse HEAD)"
GO_VERSION="$("$GO" version | awk '{print $3}')"
EXPECTED_GO_VERSION="go1.23.12"

if [[ "$GO_VERSION" != "$EXPECTED_GO_VERSION" ]]; then
  echo "SPK builds require $EXPECTED_GO_VERSION; got $GO_VERSION" >&2
  exit 2
fi
if [[ ! "$REVISION" =~ ^[0-9a-f]{40}$ ]]; then
  echo "invalid Git revision: $REVISION" >&2
  exit 2
fi
if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=all)" ]]; then
  echo "refusing provenance-bearing SPK build from a dirty working tree" >&2
  exit 2
fi

OUTPUT="${1:-$ROOT/dist/OCD-armada38x-$VERSION.spk}"
PAYLOAD_DIR="$ROOT/package-source/payload/bin"
PROVENANCE="$ROOT/package-source/payload/SOURCE_PROVENANCE.json"
mkdir -p "$PAYLOAD_DIR" "$(dirname "$OUTPUT")"

"$PYTHON" -c 'import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_bytes((json.dumps({"build_contract":"repo-contained","builder_go_version":sys.argv[5],"external_metadata_inputs":["TVmaze","TMDB","embedded-media-tags"],"logic_authority":"repository","product":"OCD","schema":"OCD_SOURCE_PROVENANCE_V1","source_repository":sys.argv[4],"source_revision":sys.argv[3],"source_revision_url":sys.argv[4]+"/commit/"+sys.argv[3],"version":sys.argv[2]},sort_keys=True,indent=2)+"\n").encode("utf-8"))' "$PROVENANCE" "$VERSION" "$REVISION" "$SOURCE_URL" "$GO_VERSION"

CGO_ENABLED=0 GOOS=linux GOARCH=arm GOARM=7 \
  "$GO" build -mod=vendor -trimpath -buildvcs=false \
  -ldflags="-s -w -buildid= -X github.com/thebrazenbeard/ocd/internal/buildinfo.Version=$VERSION -X github.com/thebrazenbeard/ocd/internal/buildinfo.Revision=$REVISION -X github.com/thebrazenbeard/ocd/internal/buildinfo.SourceURL=$SOURCE_URL" \
  -o "$PAYLOAD_DIR/ocd" "$ROOT/cmd/ocd"

"$PYTHON" "$ROOT/tools/package_contract_test.py"
PYTHONPATH="$ROOT/tools/spk_packager" "$PYTHON" -m spk_packager.cli lint "$MANIFEST"
PYTHONPATH="$ROOT/tools/spk_packager" "$PYTHON" -m spk_packager.cli build "$MANIFEST" --output "$OUTPUT"
PYTHONPATH="$ROOT/tools/spk_packager" "$PYTHON" -m spk_packager.cli verify "$OUTPUT"

sha256sum "$PAYLOAD_DIR/ocd" "$OUTPUT"
