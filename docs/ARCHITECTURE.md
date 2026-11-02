# OCD architecture — Organized & Clean Disk

## Scope and current boundary

OCD targets explicitly registered local folders on **Windows and Linux**. Local and cloud storage are part of the product direction, but the source implementation currently plans local files by explicitly registered folder or filename-extension tags—not inferred document or media content. It has on-demand scan/plan commands, an observe-only polling monitor, read-only duplicate analysis, and opt-in verified artifact download and local content-addressed cache modules. Optional startup templates can schedule the *observe-only* watcher. OCD does **not** use filesystem event notifications, synchronize remote objects, authenticate to cloud storage accounts, upload files, or operate as an automatic organizing service. macOS is not a target.

The executable path is intentionally small:

1. Read locally stored configuration of registered source/destination roots.
2. Scan eligible local files without changing them.
3. Build a deterministic move plan or a review/hold result.
4. Only on a separate `apply` command, validate and execute approved moves.

The CLI invokes these phases on demand. Observation-only polling may repeat the scan/plan phases but never invokes apply. No HTTP API, schedule, queue, filesystem-event loop, database, or daemon is required for this flow; the Python library APIs are callable independently.

## Registration contract

Each configured root has these fields:

| Field | Meaning |
|---|---|
| `source` | Explicit, existing local directory from which eligible files may be considered |
| `destination` | Explicit, existing local directory to receive organized files |
| `mode` | `observe` (default) or `apply` |
| `tags` | Explicit folder ancestry or extension-to-category mappings, such as `folder:Invoices=Accounting` and `.jpg=Photos` |
| `settle_seconds` | Minimum age for a candidate file to be considered stable |

Configuration is per-user JSON with a `version` and list of roots. User-selected paths, not broad disk discovery, define the allowed scope. Source and destination must be distinct, nonoverlapping directories, and moving between different filesystems is refused. Explicit `folder:relative/path=Category` tags match descendants by longest folder ancestry, then extension tags (such as `.pdf=Documents`) are used as fallback. Tags are not evidence that OCD recognizes file contents, extracts semantic metadata, or chooses a cloud account. Files without a matching folder or extension tag are held. Category labels are normalized as safe directory components.

## Local pipeline

### Scan (read-only)

The scanner enumerates regular files within a registered local source, sorted deterministically by absolute path. It does not intentionally follow symbolic links, junctions, or Windows reparse points, and it prunes an output destination nested inside the source as a defense in depth (although the planner refuses overlapping roots). Candidates younger than the configured settle interval are held back. Scan failures are surfaced rather than silently treated as a successful empty result; the scanner does not read file contents.

The shipped `scanner.ScanBudget` is an **opt-in Python API**, not a background watcher or CLI flag. It bounds directory count, entry count, depth, and cooperative elapsed time; detected queued-directory replacement or disappearance raises an incomplete-scan error instead of publishing a full inventory. The unbudgeted `scan_root` API retains its original behavior. Windows path traversal checks directory identity but is not an atomic defense against repeated malicious path swaps; filesystem syscalls can overrun a cooperative time limit. The durable SQLite observer in draft PR #10 is not part of the released main branch.

### Plan (read-only)

The planner routes files with an explicit matching folder or extension tag to `<destination>/<category>/<basename>`, using portable-safe component normalization. Original subdirectory hierarchy is not reproduced. A plan records the observed source path, size, and modification time so that apply can reject candidates which changed after the scan. Invalid paths, unmatched tags, path escape, case-insensitive name conflicts, multiple sources mapping to one target, or other uncertain states produce hold/review outcomes. Planning is not an implicit filesystem write or permission grant.

### Apply (mutating, explicitly gated)

An invocation must identify a registered root configured as `apply` **and** carry `--yes`. The CLI replans from current filesystem state; the applier validates the batch and rechecks source identity/size/time, path components, same-filesystem placement, and destination vacancy before each move. Existing targets are not replaced. **Linux apply** uses the `renameat2(RENAME_NOREPLACE)` no-replacement primitive, with secure directory access and an error if the primitive is unavailable. **Windows apply is currently disabled/fail-closed**; `win32.rename_noreplace` is a source-level helper under qualification, not an enabled commit path. It is limited to fixed local drive-letter paths and rejects reparse points and unsupported path forms. No deletion or automatic copy/delete fallback is part of the organizer contract.

The applier writes a local JSONL intent/completion journal beside the configuration file before and after its moves, then attempts reverse-order rollback if a batch fails. Incomplete or ambiguous journal state blocks further automatic application and calls for manual recovery. Rollback is best-effort, not an atomic multi-file transaction.

These are safety contracts, not a statement that transactional durability, power-loss recovery, network-filesystem behavior, or every Windows/Linux filesystem has been qualified. Consult [Qualification](QUALIFICATION.md) for the distinction between source implementation and tested behavior.

### Observe-only polling (`monitor.watch_roots`)

This source API repeatedly validates explicitly supplied roots, scans them, and rebuilds their full plans at a caller-specified interval. The first observation and later changes emit `change` events containing current files, proposed moves, and holds. Per-root scan/plan failures emit coalesced `error` events; other roots continue to be observed. A caller-owned stop event or bounded poll count ends the loop. No files are moved, including when a root is configured `apply`; this is polling in the invoking process, **not** an installed watcher, event-driven monitor, scheduler, or daemon.

### Read-only disk usage (`usage.scan_usage`)

`ocd usage --path ABSOLUTE_DIRECTORY` is separate from organizer registration, destination routing, and any mutating command. It inventories an explicitly selected Windows or Linux directory with processed-entry, depth, and output limits (one directory-name lookahead detects overflow); it does not read file contents, write journals, move data, or delete anything. The output includes largest observed files and top-level logical-size groups. The POSIX traversal uses directory descriptors and `O_NOFOLLOW`; Windows uses no-reparse path checks and remains subject to native path-race qualification. Symbolic links, Windows reparse points, and detected cross-device mounted directories are not traversed. Same-device bind mounts cannot reliably be detected, so reported bytes can include their contents. Windows pathname enumeration is best effort against concurrent malicious junction swaps, not yet handle-anchored or live NTFS-qualified. Rejected entries, inaccessible subtrees, exhausted budgets, and observed directory instability make the report **incomplete** and cause a nonzero CLI status.

Logical byte totals count every regular-file pathname, including hardlink aliases. POSIX allocated-byte estimates count known `(st_dev, st_ino)` identities once using `st_blocks`, and are `null` when unavailable; Windows allocation remains **unknown**, never guessed from logical size. Directory group totals overlap by hierarchy if expanded, and neither logical nor estimated allocated bytes can be equated with safe reclaimable space. Results are snapshots, not filesystem authorization or deletion recommendations. An opt-in `usage --html ABSOLUTE_OUTPUT_FILE` writes a **static, self-contained, offline HTML treemap** of observed top-level logical bytes and largest files. It refuses existing output files and requires an output path outside the selected scan root; this is the only explicit report-file creation effect and does not modify scanned files. The report has no JavaScript, network connection, live drill-down, or deletion control. A native interactive WinDirStat/SquirrelDisk-style **GUI**, persistent index, certified cleanup rules, and remote exploration are not yet shipped.

### Evidence-only cleanup review (`cleanup.review_cleanup`)

`ocd cleanup review --path ABSOLUTE_DIRECTORY` reuses the bounded `usage.scan_usage` inventory without loading organizer configuration or acquiring write authority. A **complete** scan yields a deterministic top-N list of large-file observations. Every observation is blocked from deletion and carries unknown reclaimable bytes; no remote backup, retention, inactive-use, or ownership evidence is inferred from file size. An incomplete scan returns no observations and a nonzero CLI status with issues, not a misleading partial cleanup proposal.

The command has no mutation API, deletion plan, reversible staging, automatic policy, or cloud/VN transfer integration. It is a foundation for future evidence-gated review, not a safe-to-delete recommendation.

### Exact-content duplicate candidates (`duplicates.scan_duplicates`)

This source API examines regular files beneath only the explicitly supplied source roots, independent of extension tags and settle time. It skips symlinks/reparse points, de-duplicates names referring to the same inode, and hashes same-sized candidates with streaming SHA-256. Changed/disappeared candidates are skipped; scan/root/file/entry limits bound work and over-limit calls fail rather than return a misleading partial result. Groups are **read-only findings**: neither deletion nor hard-link replacement is offered. Reported logical duplicate bytes and potential reclaim bytes are estimates, not proof of physically recoverable space; hard links, sparse files, compression, reflinks, and concurrent changes complicate that measurement.

### Verified local artifact staging (`artifacts.load_manifest`, `artifacts.fetch_verified`)

This is a separate, explicit **download-to-local-disk** operation, available from `ocd artifact fetch` or the Python API, not part of organizer routing or a cloud-sync adapter. It does not load registered organizer roots or need an organizer config. Manifest entries state `url`, expected SHA-256, exact byte `size`, and a safe relative `destination_relpath`; the command selects exactly one entry by that relative path. The downloader requires HTTPS for remote servers (loopback HTTP is allowed for local testing), bounds redirects, does not send a GitHub Release token to redirect destinations, streams and checks content before local publication, and refuses to replace an existing file. Linux uses an anchored POSIX hard-link publication path; native Windows fixed-drive NTFS/ReFS handle-based no-overwrite publication exists at source level but is **disabled by default** pending host qualification. `OCD_EXPERIMENTAL_WINDOWS_ARTIFACT_PUBLISH=1` opts in for controlled disposable-fixture tests only. It makes no remote changes, uploads, or synchronization promises.

### Independent restored-file verification (`verification.verify_local_artifact`)

`ocd artifact verify` selects one explicit manifest entry, opens an **already-existing** file beneath a selected absolute directory and streams its bytes to recheck size and SHA-256. It performs no network request or filesystem mutation. POSIX opens each ancestor with no-follow descriptors; Windows pins the fixed local NTFS/ReFS directory chain and reads a non-reparse file handle sharing **read access only**. Windows therefore refuses a concurrently open writable handle and blocks new writers/deletion during hashing. Both compare file identity/metadata around the hash and refuse unsafe or changed paths to the extent exposed by the host filesystem. A passing result is a point-in-time local content check, **not** evidence of independent remote storage, backup retention, live-use safety, or authorization to evict another copy. JSON success and failure both explicitly report deletion ineligibility.

### Content-addressed artifact cache (`cloud.LocalContentStore`)

An explicit local cache stores bytes under a deterministic SHA-256 key after validating their supplied hash and exact byte count. `ocd cache import` imports one existing file without deleting it; `ocd cache materialize` verifies one cached object and publishes a separate copy into a user-selected, existing job directory without overwriting a target. These commands are independent from organizer root registration. The `HTTPSManifestProvider` API provides **read-only, manifest-selected download retrieval** through the verified fetcher; it is not an account-authorized remote object listing, upload, cloud relocation, or automatic synchronization. Native safe cache publication remains POSIX-only/fail-closed elsewhere until separately qualified.

## Command boundary

```text
ocd [--config PATH] init
ocd [--config PATH] roots add SOURCE DESTINATION [--mode observe|apply] [--tag TAG=LABEL ...] [--settle-seconds NUMBER]
ocd [--config PATH] roots list
ocd [--config PATH] scan [--root SOURCE]
ocd [--config PATH] plan [--root SOURCE]
ocd [--config PATH] watch [--root SOURCE] [--interval SECONDS]
ocd [--config PATH] duplicates [--root SOURCE] [--max-files NUMBER] [--max-entries NUMBER]
ocd usage --path ABSOLUTE_DIRECTORY [--max-entries NUMBER] [--max-depth NUMBER] [--top NUMBER] [--json | --html NEW_ABSOLUTE_OUTPUT_FILE]
ocd cleanup review --path ABSOLUTE_DIRECTORY [--max-entries NUMBER] [--max-depth NUMBER] [--top NUMBER] [--json]
ocd [--config PATH] apply --root SOURCE --yes
ocd artifact fetch --manifest JSON --destination-root EXISTING_ABSOLUTE_DIR --artifact DESTINATION_RELPATH
ocd artifact verify --manifest JSON --destination-root EXISTING_ABSOLUTE_DIR --artifact DESTINATION_RELPATH [--json]
ocd cache import --store-root EXISTING_ABSOLUTE_DIR --source ABSOLUTE_FILE --sha256 HEX --size BYTES
ocd cache materialize --store-root EXISTING_ABSOLUTE_DIR --sha256 HEX --size BYTES --destination-root EXISTING_ABSOLUTE_DIR --destination RELPATH
```

The operations `scan`, `plan`, `watch`, `duplicates`, `usage`, `cleanup review` and `artifact verify` are read-only with respect to managed files. The foreground `watch` command polls until interrupted; the `duplicates` command prints exact-content candidates and conservative space estimates, without remediation. `apply` is the only user-facing operation allowed to **move organizer files**, and that commit path is currently Linux-only. Adding a root changes configuration, not the files inside that root. The default mode is `observe`. `artifact fetch` and `cache` are separately opted-in file-creation operations; they never move organizer sources, delete their own inputs, or upload/synchronize remote content.

## Storage and external-system boundaries

- **Local:** Ordinary filesystem paths on targeted Windows/Linux hosts. Permissions are those of the invoking OS user; OCD is not a privileged volume manager. Host qualification is incomplete. Windows organizer `apply` remains disabled; experimental explicit artifact downloads have a disabled-by-default native Windows path.
- **Cloud (future):** Full provider adapters and credentials need separate explicit authorization, object identity/version checks, conflict policy, and move semantics. The existing HTTPS manifest reader and local content-addressed cache are retrieval/staging building blocks; they do not implement cloud-provider accounts, authenticated object browsing, upload, remote moves, or sync.
- **Monitoring:** Observe-only polling exists; an unattended event watcher would additionally need event handling, reconciliation, loop prevention, restart recovery, and deployment/service management. There is no always-on monitoring guarantee or automatic reorganization.
- **Model-worker custody:** Code, pinned manifests, hashes, and retrieval instructions can live in Git while large model weights live in a separate artifact store. For current private BT2 GitHub **draft** releases, read-only worker accounts cannot list the drafts (GitHub limits draft-release visibility to push-access accounts). Publication *within the private repository* needs separate authorization, and each worker must independently authenticate, download, verify bytes, and load the needed model before eviction of a retained local fallback. OCD's verifier alone cannot prove remote retention.
- **DSM/media (historical):** Prior design notes describe Synology packages, TVmaze/TMDB and typed media renaming. Those are not current Python CLI features, and there is no supported DSM install/API path here.

## Build and installation qualification

The project produces a Python wheel with an `ocd` console entry point. CI on Windows and Ubuntu with Python 3.12/3.13 builds that wheel, installs it into a fresh virtual environment **outside the checkout**, and verifies both `python -m ocd --help` and installed `ocd --help`. This is evidence that the CLI package is installable, **not** evidence of a native installer, system service, cloud backend, or safe unattended application. See `tests/verify_wheel_install.py` and [Qualification](QUALIFICATION.md).

## Non-goals and reliability limits

OCD does not promise automatic content classification, metadata enrichment, remote cloud sync, duplicate cleanup, backup, guaranteed atomic multi-file transactions, or recovery of files modified by third-party processes. Exact-content duplicate *reports* do not authorize deletion. Artifact downloads are opt-in local staging, not backup or synchronization. Users should inspect plans and test on copies before moving valuable files.
