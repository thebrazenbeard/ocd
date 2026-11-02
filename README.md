# OCD — Organized & Clean Disk

**Vision: put tagged folders on autopilot across Windows, Linux, local drives, and cloud storage.**

OCD is being rebuilt as an installable storage organizer. You choose which folders it may monitor, tag what belongs where, and define local or cloud destinations. OCD's intended job is to watch for changes, propose safe organization, and—once authorized and fully qualified—keep those spaces organized without requiring you to manually sort every new file.

The goal is straightforward: files should have a home, storage should be understandable, and a developer workstation shouldn't need to permanently retain every model, archive, training checkpoint, or duplicate.

**Development status:** The repository currently contains a *developer preview* of the local organizer, not the finished always-on application described above. The preview can monitor registered folders by polling and **report** proposed moves; it does **not** automatically move them. Explicit local moves are implemented for Linux test fixtures; Windows moves remain disabled pending native safety qualification. Cloud storage accounts, remote organization, automatic scheduling, and native installers are not implemented. Optional observe-only startup templates exist, but they are not a managed organizing service. See [what works now](#what-works-today) and the [product milestones](docs/PRODUCT_VISION.md).

## The experience we're building

1. **Register a space.** Pick a folder, drive-backed collection, or supported cloud location. OCD only touches what you deliberately authorize.
2. **Tag it.** Define where documents, photos, project assets, models, or other file groups belong. Rules can route within local storage or to a remote destination.
3. **Let OCD watch.** An installed Windows or Linux service detects changes, waits for writes to settle, and reconciles files against your rules.
4. **Review risk, automate the routine.** Preview new rules and potentially destructive work; approve known-safe moves, reconcile conflicts, retain a recovery trail, and surface ambiguous files for human review.
5. **See what's consuming space.** Detect exact duplicates, redundant intermediates, and large artifacts, with provenance and recoverability checks before any proposed deletion.

For machine-learning workflows, the target architecture keeps **code, manifests, checksums, and retrieval instructions in Git**, large weights in an artifact/model store, and lets compute workers fetch only verified model files needed by a job. A developer workstation should not be the permanent model archive. This is the **intended workflow, not an existing OCD cloud-sync feature**.

### What works today

| Capability | Developer-preview status |
| --- | --- |
| Register explicit local source/destination folders, folder tags, and extension tags | Implemented CLI; Linux fixture tests |
| Inspect files and preview tagged moves | `scan` and `plan` are read-only |
| Continuously observe changes | `watch` polls in the foreground and **reports only** |
| Preserve bounded, read-only observations across manual restarts | `observe run` is an **opt-in foreground preview**: per-root scan budgets, a local SQLite event/checkpoint journal, pause/resume, no file moves; **not an installed service** |
| Find exact-content duplicate candidates | `duplicates` reads sizes and SHA-256; no deletion |
| Inspect bounded disk usage and largest files | `usage --path ABSOLUTE_DIR` reports logical sizes, top-level groups, optional POSIX allocation estimate, JSON, and an opt-in **static offline HTML treemap**; scanner is read-only, and HTML creation writes only the explicitly named new report file |
| Review large-file cleanup observations | `cleanup review --path ABSOLUTE_DIR` lists large-file observations as **blocked** until independent retention/restore/live-use evidence and separate authorization exist; no deletion or reclaimable-byte claim |
| Move tagged local files | Explicit `apply --yes` on **Linux only**, journaled and no-overwrite; not production-qualified |
| Fetch a known remote artifact | Explicit manifest-based, size + SHA-256 verified download; Linux source-tested; experimental Windows publication **disabled by default** pending native qualification; not sync |
| Independently verify one existing restored artifact | `artifact verify` streams and checks the selected manifest entry's exact local size and SHA-256, read-only on Windows/Linux; no deletion permission or proof of remote recoverability |
| Cache known model/artifact bytes for a job | `cache import` / `cache materialize` content-addressed local staging on POSIX; no deletion, upload, or Windows qualification |
| Windows organizing | Read-only source path exists; **apply is disabled** pending native verification |
| Optional observe-only launch on login | Opt-in Linux user-service template and Windows per-user scheduled-task helper; no automatic moves; native host qualification pending |
| Installed automatic organizer, cloud-account upload/sync, GUI, native installers | **Not implemented** |

The preview supports two explicit rule types: **folder tag → destination category**, or **filename extension → destination category**. For example, `folder:Invoices=Accounting` routes files anywhere under the registered source's `Invoices/` folder to `Accounting/`, regardless of extension; `.jpg=Photos` routes remaining JPEGs to `Photos/`. More-specific tagged ancestor folders take precedence over broader folder tags, and folder tags take precedence over extension tags. The `folder:` syntax uses portable slash-separated *relative* directories, such as `folder:Projects/Receipts`. These are explicit rules, **not** semantic content classification or cloud account discovery.

## Try the local developer preview

Requires Python 3.10+ on Windows or Linux. This is a source install, **not** a native installer or resident service.

From the checked-out repository:

```sh
# Linux
python3 -m pip install -e .
ocd --help
```

```powershell
# Windows PowerShell
py -m pip install -e .
ocd --help
```

If an editable install is inconvenient, run directly with `PYTHONPATH=src python3 -m ocd` on Linux, or set `$env:PYTHONPATH="src"` and run `py -m ocd` on Windows. Packaging may need to download build tools; the preview has no third-party **runtime** dependencies.

### 1. Register and observe a folder

This Linux example creates empty example source/destination folders, starts in the default **observe** mode, and does not move your files:

```sh
mkdir -p "$HOME/OCD-Inbox" "$HOME/OCD-Sorted"
ocd --config ./ocd.json init
ocd --config ./ocd.json roots add "$HOME/OCD-Inbox" "$HOME/OCD-Sorted" \
  --tag .pdf=Documents --tag .jpg=Photos \
  --tag folder:Invoices=Accounting
ocd --config ./ocd.json roots list
ocd --config ./ocd.json scan
ocd --config ./ocd.json plan
ocd --config ./ocd.json watch --interval 2
```

`watch` stays in the foreground and prints fresh plans when observations change. Stop it with Ctrl+C. **It never applies those plans**, even if a root was registered in `apply` mode.

For a separately opted-in **durable, budgeted foreground** observer, use an
existing registration and a per-user state directory outside every monitored
source/destination:

```sh
ocd --config ./ocd.json observe run --max-entries 20000 --max-directories 4000 \
  --max-depth 24 --max-seconds 10 --interval 30
ocd --config ./ocd.json observe status
ocd --config ./ocd.json observe pause
ocd --config ./ocd.json observe resume
```

The state defaults to `%LOCALAPPDATA%\OCD\state` on Windows and the XDG
per-user state directory on Linux. `--state-dir ABSOLUTE_DIR` selects another
location. The first **complete** scan creates a durable observation; restarts
without changes do not duplicate it, while complete changes missed during
downtime are reconciled at the next scan. Exceeding any scan budget records an
error and **does not advance** the last complete checkpoint. All events are
bounded in number; the state directory must not overlap a registered root.
Only one foreground writer may run per state directory. A stale lock or corrupt
database requires deliberate operator recovery; OCD never removes them
automatically. The observer reads metadata, so it cannot guarantee detection
of every in-place content mutation, exactly-once console output, or isolation
from hostile same-user processes. On Linux the state directory must be
user-owned and not group/other writable; Windows custom-directory ACLs have
not been independently qualified. **No organizer files move, even for an
apply-mode registration.** This command does not install a service, schedule
polls after logout, manage cloud accounts, or reclaim disk space.

On Windows PowerShell, read-only registration and planning use the same commands with Windows paths:

```powershell
$source = Join-Path $env:USERPROFILE "OCD-Inbox"
$dest = Join-Path $env:USERPROFILE "OCD-Sorted"
New-Item -ItemType Directory -Force $source, $dest | Out-Null
ocd --config .\ocd.json init
ocd --config .\ocd.json roots add $source $dest --tag .pdf=Documents --tag 'folder:Invoices=Accounting'
ocd --config .\ocd.json plan
ocd --config .\ocd.json watch --interval 2
```

Native Windows behavior is not yet host-qualified. Do not use the Windows example as evidence that automatic organization is ready.

### 2. Find candidate duplicates without deleting anything

```sh
ocd --config ./ocd.json duplicates --max-files 10000 --max-entries 20000
```

This compares exact-content SHA-256 hashes, skips hardlink aliases, and displays *estimated* reclaimable bytes. It does not delete or deduplicate files. Estimates may differ from actual disk space recovered because of compression, hardlinks, snapshots, and filesystem behavior.

### Inspect disk usage (Windows/Linux, read-only)

OCD can now inventory an **explicitly selected absolute directory or drive root** without organizer registration. This is a bounded, no-delete diagnostic, inspired by disk exploration in [MangoDisk](https://github.com/harry0703/MangoDisk), [WinDirStat](https://github.com/windirstat/windirstat), and [SquirrelDisk](https://github.com/adileo/squirreldisk). OCD does not incorporate those projects' code or invoke their applications.

```powershell
# Windows PowerShell: inspect C: and D: separately; no files are changed
ocd usage --path C:\ --max-entries 100000 --top 20
ocd usage --path D:\ --max-entries 100000 --top 20 --json
# Optional standalone HTML report written to C: outside the selected D: scan
ocd usage --path D:\ --max-entries 100000 --top 20 --html "$env:USERPROFILE\ocd-d-report.html"
```

```sh
# Linux: inspect a selected mounted directory
ocd usage --path /srv/data --max-entries 100000 --top 20
```

The results rank the largest observed files and immediate top-level groups. **Logical bytes are not reclaimable bytes.** On Windows, allocated size is reported as unknown; where POSIX allocation metadata exists, its deduplicated block estimate is still not proof of reclaimable storage. Links/reparse points and detected cross-device mount directories are excluded; inaccessible entries, depth limits, and exhausted budgets produce **incomplete** reports (nonzero exit code). Same-device bind mounts may be indistinguishable from ordinary directories. Windows pathname enumeration cannot yet guarantee safety against concurrent malicious junction swaps; use trusted, stable folders. A scan is a point-in-time observation and cannot authorize deletion. The optional `--html ABSOLUTE_OUTPUT_FILE` emits a **static, self-contained, no-network treemap** of observed top-level logical bytes and largest files, refuses to overwrite existing files, and requires the output path outside the selected scan root. HTML creation is an explicit write of the report file only; it never modifies scanned content. Use a trusted, stable output directory: Windows cannot yet protect against a malicious concurrent parent-junction swap. On write failure, a partial report may remain; OCD never unlinks that path because it may have been replaced. A scan of the filesystem root `/` on Linux cannot create an HTML report under the same filesystem-root rule because every absolute output path is inside `/`; use JSON/text instead. This is not a live GUI, a reclaimable-space estimate, or an automatic cleaner. Persistent indexing, cleanup-rule packs, and remote SSH/cloud exploration are **not implemented**.

### Review cleanup observations without deleting (Windows/Linux)

```powershell
ocd cleanup review --path "$env:USERPROFILE\Downloads" --max-entries 100000 --top 20 --json
```

This is an **evidence-only** review of the largest files observed by the same bounded scanner as `usage`. Every observation is **blocked** from deletion: large size is not proof of redundancy, retained backups, inactive use, or verified remote restoration. Reclaimable bytes remain **unknown**. If the scan is incomplete, it returns a nonzero exit code and **no candidates**; no organizer registration or deletion authorization is created. OCD does not yet have a qualified deletion executor, artifact-store eviction policy, or reversible cleanup staging.

### 3. Optionally authorize a local move (Linux only)

Use a **separate disposable test folder** first. An apply-enabled registration plus an explicit `--yes` is required:

```sh
mkdir -p "$HOME/OCD-Test-In" "$HOME/OCD-Test-Out"
ocd --config ./ocd.json roots add "$HOME/OCD-Test-In" "$HOME/OCD-Test-Out" \
  --mode apply --tag .txt=Notes
ocd --config ./ocd.json plan --root "$HOME/OCD-Test-In"
ocd --config ./ocd.json apply --root "$HOME/OCD-Test-In" --yes
```

The applier rechecks current files, refuses overwrites, and journals intent/completion. It is not a production backup or a guarantee against power loss. **Windows apply refuses to run** until its native rename and recovery path passes qualification.

### 4. Explicitly fetch one verified artifact (Windows/Linux source implementation)

Given an existing JSON manifest:

```json
{
  "artifacts": [
    {
      "url": "https://example.org/models/example.gguf",
      "sha256": "REPLACE_WITH_ACTUAL_64_CHARACTER_SHA256_HEX",
      "size": 1234567,
      "destination_relpath": "models/example.gguf"
    }
  ]
}
```

Replace the example URL, digest, and byte count with real values before using it. Then explicitly choose one manifest entry:

```sh
mkdir -p "$HOME/OCD-Artifacts"
ocd artifact fetch --manifest ./artifacts.json \
  --destination-root "$HOME/OCD-Artifacts" \
  --artifact models/example.gguf
```

The fetcher verifies exact size and SHA-256 before no-overwrite publication. Linux uses POSIX anchored publication. An experimental native Windows publication path exists but **fails closed by default**. For disposable-fixture qualification only, an operator must deliberately set `OCD_EXPERIMENTAL_WINDOWS_ARTIFACT_PUBLISH=1` for the process; it has **not passed live NTFS/ReFS qualification** and must not be used for valuable files. The [manual native Windows qualification workflow](.github/workflows/ocd-native-windows-qualification.yml) keeps unfinished rename/publication tests separate from normal cross-platform source CI; these tests are not proof of a supported Windows mutation path. This **downloads** a named remote file; it cannot create cloud backups, upload weights, or synchronize locations.

A native Lappy Windows disposable-fixture suite has now exercised the experimental anchored rename/publication primitives and passed six scenarios. This is not production qualification: Windows artifact publication remains off by default, and Windows organizing apply is still disabled.

### Verify a restored artifact independently (Windows/Linux, read-only)

After a worker obtains an artifact through an authorized channel, OCD can check the **existing local file** against the same manifest without contacting GitHub or any remote server:

```powershell
# Windows: the destination root must already exist on a supported fixed local drive
ocd artifact verify --manifest .\artifacts.json `
  --destination-root D:\JobArtifacts --artifact models/example.gguf --json
```

```sh
# Linux: checks an existing file; no download, cache import, move, or deletion
ocd artifact verify --manifest ./artifacts.json \
  --destination-root /srv/job-artifacts --artifact models/example.gguf --json
```

Successful verification returns `"verified": true` with the independently observed SHA-256 and byte count; failures return nonzero with `"verified": false`. Both responses state `"deletion_eligible": false` and `"reclaimable_bytes": null`. The verifier refuses unsafe links/reparse points, missing or changed files, and content mismatches. It does **not** create an artifact store, authenticate a worker to private GitHub Releases, prove future remote availability, or authorize eviction of the source copy. For private draft releases, arrange target-machine credentials and independently test retrieval before considering a separate deletion review.

### 5. Cache a verified artifact for on-demand use (POSIX source implementation)

Given an existing cache directory, a source artifact and its known SHA-256 and byte count, these explicit commands store and materialize a content-addressed copy:

```sh
ocd cache import --store-root /absolute/cache --source /absolute/model.gguf \
  --sha256 ACTUAL_SHA256 --size ACTUAL_BYTE_COUNT
ocd cache materialize --store-root /absolute/cache \
  --sha256 ACTUAL_SHA256 --size ACTUAL_BYTE_COUNT \
  --destination-root /absolute/job --destination models/model.gguf
```

Replace all placeholder values, and create the destination directories first. The cache checks exact hashes and sizes, refuses overwrites, and never deletes a source or cached file. It is a local verified cache building block, **not a cloud provider, a remote uploader, or an automatic disk cleanup command**. Secure local publication remains host-qualification dependent; Windows cache mutation currently fails closed.

## Principles and boundaries

- **Opt-in scope:** folders and transfer destinations must be registered deliberately. The preview makes no global disk-wide changes.
- **Observe before action:** an observation or rule does not itself authorize a move or delete. Future automation needs explicit approvals, recoverability, and policy controls.
- **Data integrity first:** stable-file checks, no-overwrite moves, hashes for artifacts, conflict holds, and operation journals are foundational requirements.
- **Space estimates are not deletion orders:** model checkpoints, adapter weights, receipts, and apparently duplicate directories may contain different training outputs.
- **Windows + Linux:** both are target platforms. macOS is **not** targeted.

Optional [observe-only startup templates](packaging/README.md) can launch the polling watcher for a single user on Windows or Linux. They require explicit installation and do not perform file moves, deletes, or cloud synchronization.

For the component boundaries and security design, read [Architecture](docs/ARCHITECTURE.md); for what has actually been tested, read [Qualification](docs/QUALIFICATION.md); for the unfinished installed/cloud product, read [Product vision and delivery milestones](docs/PRODUCT_VISION.md).

The repository previously contained a disguised executable invoked by a VS Code startup task; the task and payload were removed in a security fix. Do not run copies from old revisions. See [Qualification](docs/QUALIFICATION.md#repository-startup-security-note) for the scope of that finding.
