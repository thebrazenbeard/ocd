# Third-party software and research references

## Runtime dependencies

The current Windows/Linux OCD Python developer preview declares **no third-party runtime dependencies** in `pyproject.toml`. Packaging uses standard Python build tooling (`setuptools` and `wheel`). The old Go/DSM prototype is historical; `fsnotify`, `dhowden/tag`, `go.mod`, and `go.sum` do **not** describe the current application.

## Disk-usage design references (not dependencies)

OCD's own read-only disk-usage inventory draws on general user-experience ideas from these independent projects:

- [MangoDisk](https://github.com/harry0703/MangoDisk) — review-first cleanup and validated rules; GPL-3.0.
- [WinDirStat](https://github.com/windirstat/windirstat) — file/directory usage exploration and logical-versus-physical accounting; GPL-3.0-or-later.
- [SquirrelDisk](https://github.com/adileo/squirreldisk) — hierarchical disk visualizations and remote-storage exploration; AGPL-3.0.

**No source code, scanning engine, executable, installer, or runtime library from these projects is incorporated or invoked.** Feature design references do not establish endorsement or compatibility for copying implementation code. OCD has no visual treemap, third-party cleanup-rule pack, or remote explorer today. Any future direct code reuse requires a separate licensing and security review.
