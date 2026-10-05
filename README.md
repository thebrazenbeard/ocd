# OCD — Organized & Clean DiskStation

OCD is a low-overhead Synology DSM daemon that watches explicitly registered media roots and keeps new or changed media organized without turning the NAS into a full Sonarr/Radarr stack.

**OCD never guesses a library type.** Every watched root is explicitly registered as television, movies, or music because the parsing, metadata, and naming contracts are different.

## Source and reasoning authority

This repository is OCD's semantic authority. Parsing, matching, ambiguity handling, naming policy, safety decisions, mutation planning, journaling, watcher behavior, reconciliation, and API/service behavior all live here. The SPK is a compiled deployment of this repository, not a separate reasoning service.

External metadata services are inputs, not policy engines: TVmaze can supply television metadata, TMDB can supply movie metadata, and embedded media tags supply v0.1 music metadata. OCD does not require BT2, Project Runner, a companion repository, or a remote database/agent for its control logic.

Every provenance-bearing SPK embeds the exact Git revision it was built from and links that runtime back to `https://github.com/thebrazenbeard/ocd`.

See `docs/SELF_CONTAINED_RUNTIME.md`.

## Download

Installable SPKs are published as GitHub Release assets:

https://github.com/thebrazenbeard/ocd/releases/latest

Each release also contains `SHA256SUMS`. Branch CI artifacts are qualification evidence, not the durable download surface.

## Naming contracts

Television:

```
Show/
└── Season 01/
    └── S01E01 - Episode Name.ext
```

Movies:

```
Title (Year)/
└── Title (Year).ext
```

Music:

```
Artist/
└── Album/
    └── Song Title.ext
```

Track number and disc number stay in embedded music metadata; OCD does not put them in the filename.

## Metadata contracts

- **Television:** TVmaze public API. No API key required.
- **Movies:** TMDB. The user supplies a TMDB API Read Access Token.
- **Music:** embedded Artist / Album / Title tags are authoritative in v0.1. Track/disc/year remain metadata and are not rewritten.

Metadata unavailability may prevent a metadata-dependent rename. It does not delegate OCD's policy or mutation authority to the provider.

## Safety model

OCD defaults to **observe** mode. It plans and records changes but does not rename until a root is explicitly placed in **apply** mode.

Apply mode is fail-closed:

- package-user read/write access is checked before root admission;
- files must be stable before processing;
- ambiguous or missing metadata is held for review;
- existing targets are never overwritten;
- targets may not escape the registered root;
- OCD uses same-filesystem rename only, never copy/delete fallback;
- video sidecars with matching stems are moved as one operation set;
- partial apply attempts are rolled back where possible;
- every apply has an intent/completion JSONL journal;
- OCD-originated watcher events are temporarily suppressed to prevent loops.

## DSM package behavior

The DSM 7.2.2+ SPK runs as the internal `OCD` package user. During first installation, the wizard asks for:

1. an initial DSM shared-folder name;
2. its media type: television, movies, or music;
3. observe or apply mode.

The SPK declares that share through DSM's `data-share` resource worker and grants the internal OCD package user read/write access. DSM 7 creates a package share symlink under `/var/packages/OCD/shares/`; the installer resolves that to the real shared-folder path before storing the root.

Additional roots may be added later through the local API/CLI, but the OCD package user must separately be granted DSM shared-folder ACL access to those shares.

The administration/API server is loopback-only by default at `127.0.0.1:9157`. This is deliberate: v0.1 does not expose unauthenticated mutation endpoints to the LAN.

## CLI

```text
ocd serve
ocd root add --path /volume1/TV\ Shows --type tv --mode observe
ocd root list
ocd root remove --id <id>
ocd tmdb --bearer <TMDB_API_READ_ACCESS_TOKEN>
ocd version
```

## Repository-contained build

OCD contains the packaging logic and pinned library source needed for its own build. It does not clone `spk-packager` or fetch Go module source during the build.

Required local toolchains: Go 1.23.12 for byte-reproducible SPK builds and Python 3.11+. Source tests also run against newer supported Go versions in CI.

Linux/macOS/CI:

```text
go test -mod=vendor ./...
go vet -mod=vendor ./...
bash scripts/build-spk.sh
```

Windows Command Prompt:

```text
go test -mod=vendor ./...
go vet -mod=vendor ./...
.\scripts\build-spk.cmd
```

The SPK build refuses a dirty working tree because an artifact claiming an exact source revision must actually correspond to that revision. `ocd version` and `/api/v1/status` expose the bound source revision.

See `docs/ARCHITECTURE.md`, `docs/RESEARCH.md`, `docs/DSM_PERMISSIONS.md`, and `docs/SELF_CONTAINED_RUNTIME.md`.

## Qualification states

`SOURCE_VALIDATED != SPK_STRUCTURALLY_VERIFIED != SPK_REPRODUCIBLE != DSM_INSTALLED != DSM_RUNTIME_VERIFIED != APPLICATION_BEHAVIOR_VERIFIED`

A green build is not a live-NAS claim.
