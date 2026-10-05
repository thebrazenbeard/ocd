from __future__ import annotations

import json
from pathlib import Path

from .assets import write_placeholder_icon
from .model import validate_package_id


_MANIFEST = """[compatibility]
profile = "dsm-7.2.2+"
strict = true

[package]
id = {package_id}
display_name = {display_name}
version = "0.1.0-0001"
description = "Example service packaged with SPK Packager."
maintainer = {maintainer}
arch = ["noarch"]
os_min_ver = "7.2-72806"
thirdparty = true
precheckstartstop = true

[service]
enabled = true
command = "bin/example-service"
args = []
pid_file = "example-service.pid"
log_file = "example-service.log"
state_dirs = ["state"]
start_probe_seconds = 1
stop_timeout_seconds = 10

[privilege]
run_as = "package"
username = {package_id}

[assets]
icon64 = "assets/PACKAGE_ICON.PNG"
icon256 = "assets/PACKAGE_ICON_256.PNG"

[[payload.files]]
source = "payload/bin/example-service"
destination = "bin/example-service"
mode = "0755"
"""

_SERVICE = """#!/bin/sh
set -eu
trap 'exit 0' TERM INT
while :; do
    sleep 60
done
"""


def _toml_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{field_name} may not contain NUL")
    return json.dumps(value, ensure_ascii=False)


def scaffold(path: Path, package_id: str, display_name: str, maintainer: str) -> Path:
    validate_package_id(package_id)
    encoded_package_id = _toml_string(package_id, "package_id")
    encoded_display_name = _toml_string(display_name, "display_name")
    encoded_maintainer = _toml_string(maintainer, "maintainer")

    path = path.resolve()
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"target directory is not empty: {path}")
    (path / "payload" / "bin").mkdir(parents=True, exist_ok=True)
    (path / "assets").mkdir(parents=True, exist_ok=True)
    manifest = path / "spk-packager.toml"
    manifest.write_text(
        _MANIFEST.format(
            package_id=encoded_package_id,
            display_name=encoded_display_name,
            maintainer=encoded_maintainer,
        ),
        encoding="utf-8",
        newline="\n",
    )
    (path / "payload" / "bin" / "example-service").write_text(
        _SERVICE, encoding="utf-8", newline="\n"
    )
    write_placeholder_icon(path / "assets" / "PACKAGE_ICON.PNG", 64)
    write_placeholder_icon(path / "assets" / "PACKAGE_ICON_256.PNG", 256)
    return manifest
