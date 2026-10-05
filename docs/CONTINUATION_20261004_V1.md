# OCD Chat Continuation — 2026-10-04 V1

Continuation command:

`OCD::RESTORE_AND_RUN::V0_1_DSM_LIVE_QUALIFICATION_20261004_V1`

When that command is supplied in a new chat, recover current state from GitHub before acting. Do not rely on conversation memory as authority.

## Canonical project state

- Repository: `thebrazenbeard/ocd`
- Development branch: `build/ocd-v1-sol-20261004`
- Draft PR: #1 — `Build OCD v0.1 DSM media organizer`
- Architecture baseline on `main`: `30c49f0a5ebe99d217e66e985893b3b109dc2e2f`
- Executable implementation parent before this continuation checkpoint: `21b2919134daa4ea45f57df8e25561c33eb97e6b`
- Qualified SPK Packager source pinned by CI: `thebrazenbeard/spk-packager@dbda5bca7a18d919346f52d8401a00c6fd530b88`

Always fetch/read the live branch and PR first because the branch may advance after this checkpoint.

## Product concept

OCD means **Organized & Clean DiskStation**.

It is a low-overhead DSM daemon that watches explicitly registered media roots and organizes files without becoming a Sonarr/Radarr replacement. Library type is never inferred: every root is explicitly one of `tv`, `movie`, or `music`.

### User-frozen naming contracts

Television:

`Show/Season NN/SxxExx - Episode Name.ext`

Movies:

`Title (Year)/Title (Year).ext`

Music:

`Artist/Album/Song Title.ext`

Track/disc numbers stay in embedded music metadata and are not added to music filenames.

## Metadata contracts

- Television: TVmaze public API, no user credential.
- Movies: TMDB using a user-supplied API Read Access Token.
- Music: embedded Artist / Album / Title tags are authoritative in v0.1 using `dhowden/tag`.
- MusicBrainz is future enrichment/repair work, not required for v0.1.

## Safety model

- Roots default to `observe`.
- `apply` must be explicit.
- Apply mode requires package-user read/write access.
- Files must pass stability checks.
- Missing/ambiguous metadata is held.
- Existing targets are never overwritten.
- Targets may not escape their registered root.
- Rename is same-filesystem only; no copy/delete fallback.
- Matching video sidecars move with the video.
- Apply operations journal intent/completion.
- Partial batches attempt rollback.
- OCD-generated watcher events are suppressed briefly to prevent loops.
- Nested/overlapping roots are refused.

## Runtime architecture already implemented

- Go daemon.
- Recursive `fsnotify` / Linux inotify watches.
- Periodic reconciliation for missed events/restarts/watch limitations.
- Typed persistent config in `config.json`.
- Mutation journal in `journal.jsonl`.
- In-memory plans/findings for operational UI state.
- Loopback-only HTTP/API at `127.0.0.1:9157`.
- API supports health/status, roots, root mode changes, plans, findings, TMDB token update, and reconcile.
- Local HTML administration UI is embedded in the daemon.

## DSM/SPK architecture already implemented

Package ID: `OCD`. DSM 7 service account: `sc-OCD`.

Target baseline: DSM 7.2.2+, DS216 / `armada38x` first live target.

The package runs as the DSM internal package user, not root.

Install wizard asks for:
1. DSM shared-folder name;
2. media type: television / movies / music;
3. mutation mode: observe / apply;
4. optional TMDB bearer token.

The SPK uses DSM's `data-share` resource worker to grant the DSM internal service account `sc-OCD` read/write access to the initial share. `postinst` resolves `/var/packages/OCD/shares/<share>` and stores that root. Additional roots require `sc-OCD` to have DSM shared-folder ACL access.

SPK manifest version is currently `0.1.0-0001`.

## Historical qualification evidence

The earlier exact implementation subject `21b2919134daa4ea45f57df8e25561c33eb97e6b` was structurally/reproducibly qualified before the DSM 7 `sc-OCD` package-account correction. It is historical evidence only and must not be selected for live installation. Always qualify and use the current branch head.

Local:
- `go test ./...`: PASS.
- `go vet ./...`: PASS.
- SPK Packager lint: PASS.
- strict SPK verify: PASS.
- two SPK builds byte-identical: PASS.

GitHub:
- CI run #2 on the implementation head: PASS.

Qualified artifacts:
- ARMv7 OCD binary SHA-256: `3e2d82ee0fadfbf3d6873cc7ea7fc1e2a072573491dcb0ea54075a1ae2ade058`
- OCD SPK SHA-256: `7a9fcea97bafc940eb1593eca957499863c16cca07d1fe9500385e8f7d097ebe`
- SPK architecture: `armada38x`
- payload ELF `e_machine`: `40`

The hashes above belong only to that historical subject. They must not be reused as evidence for a newer commit.

## Qualification state

For the current branch head, source/build/package qualification must be established by exact-head CI and/or local readback before installation. Live DSM states remain:

- DSM_INSTALLED: PENDING
- DSM_RUNTIME_VERIFIED: PENDING
- APPLICATION_BEHAVIOR_VERIFIED: PENDING

No lower state implies a higher state.

## Next execution frontier

1. Fetch current `build/ocd-v1-sol-20261004` and PR #1; reconcile any newer work before editing.
2. Re-run exact-head CI/build verification if the branch has moved.
3. Obtain/download the exact qualified SPK artifact for the current head.
4. Install OCD on the user's DS216 / DSM 7.2.2 target using the existing live DSM access path.
5. During installation, select an appropriate existing shared folder and explicitly choose its type. Prefer `observe` for first live qualification unless the live user explicitly selects `apply`.
6. Verify installed INFO/version/arch/artifact identity.
7. Verify DSM package unit, daemon process identity, package-user identity, loopback `/healthz`, `/api/v1/status`, and root configuration.
8. Measure CPU/RSS overhead.
9. Exercise observe behavior on controlled media fixtures for TV, movie, and music naming.
10. Only after observed plans are correct, exercise controlled apply behavior; verify no overwrite, sidecar movement, journal receipts, watcher suppression, and restart/reconciliation.
11. Update `docs/QUALIFICATION.md` with exact artifact/runtime evidence.
12. Keep PR #1 draft unless the live user separately authorizes merge to `main`.

## Authority carried by the continuation command

The user has asked to continue building OCD and make the project live. Pasting the continuation command in a new chat is live authorization to resume implementation, package qualification, and OCD installation/runtime qualification on the user's DSM target as needed. It is not blanket authority to merge to `main`, delete data, weaken safety checks, expose the unauthenticated API to the LAN, or modify unrelated packages.

## Public-repo hygiene

Do not commit:
- NAS IP addresses;
- SSH keys/passwords;
- TMDB tokens;
- private share contents;
- other machine-specific credentials.

Recover live-machine connection details from the user's authorized local environment when needed.
