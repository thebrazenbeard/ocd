# OCD qualification and evidence

Qualification applies to the **current Organized & Clean Disk local CLI and its optional source modules**, not the earlier DiskStation/Go/SPK proposal. No lower verification state implies a higher one. Passing source tests on Linux, exercising a mock Win32 API, building a package, or previewing a plan does not establish live Windows/Linux host safety.

## Current claim boundaries

| Capability or verification gate | Status | Evidence required to claim verified |
|---|---|---|
| Python source imports, package entry point, and CLI commands | Source implementation; combined revision check pending | Run help/init and subcommand smoke tests after the full CLI integration from a clean checkout |
| Registered local roots, folder/extension tags, observe default, scanner, planner | Source implementation and Linux-side fixture tests exist; no host qualification | Config persistence and CLI round trip; folder-ancestor precedence, settled/unsettled files, symlinks, collisions, inaccessible paths |
| Linux apply with no overwrite or deletion | Source implementation and controlled Linux fixture tests exist; not production-qualified | `renameat2`/filesystem compatibility, target races, changed files, journal failures, interruption and recovery |
| Windows apply and native rename helper | **Apply disabled/fail-closed.** Win32 helper is source-only; mocked tests are not native qualification | Live named Windows build and NTFS/ReFS; fixed drives, reparse attacks, concurrent hardlinks, path races and journal behavior before enabling |
| Observe-only polling (`monitor.watch_roots`) | Source API and fixture tests; not an unattended watcher | Final revision test run and platform-specific callback/error/stop behavior; no mutation while polling |
| Read-only duplicate reports (`duplicates.scan_duplicates`) | Source API; final revision test and host qualification pending | Hash correctness, hardlink identities, changed files, root/path races, limits, no mutation, and estimate semantics |
| Verified local artifact download (`artifact fetch`, `artifacts.fetch_verified`) | CLI and Python API source implementation; Linux POSIX publication; **Windows native publication disabled by default** and unqualified on real host; exact `OCD_EXPERIMENTAL_WINDOWS_ARTIFACT_PUBLISH=1` required for experimental fixture tests | Exact manifest-entry CLI selection without organizer config; HTTPS/redirect/token tests, size/hash mismatch, existing targets, symlink races, failures and partial cleanup; live Windows NTFS/ReFS native fixture tests required |
| Local content-addressed artifact cache (`cache import`, `cache materialize`) | Explicit CLI and Python source implementation on POSIX; no remote uploading or deletion; Windows safe publication currently fails closed | Live POSIX fixtures, adversarial identity/hardlinks/collisions and interruption tests; Windows needs separate native qualification |
| Read-only HTTPS manifest provider (`cloud.HTTPSManifestProvider`) | Source-level retrieval wrapper around explicit artifact fetch; no account browser, remote uploader, or sync | Exact manifest selection, provider trust, redirects, token isolation, error propagation and verified restore fixture |
| Linux end-to-end behavior | **Not yet qualified** | Named distro, Python version, filesystem, exact commands, fixture, output and recovery results |
| Windows read-only CLI behavior | **Not yet qualified** | Named Windows build, Python version, filesystem, actual registration/scan/plan/watch/duplicate commands and results |
| Event-driven watcher, scheduling, or managed daemon | **Not implemented** | Implementation plus event/restart/reconciliation/deployment testing |
| Cloud provider integration, remote sync/moves or uploads | **Not implemented** | Provider-specific credentials, object version, conflict, retry and failure tests; artifact download does not qualify these |
| Cross-platform GitHub Actions matrix | Workflow configured for Ubuntu/Windows and Python 3.12/3.13; **runner results not established here** | Record actual run URL, commit and per-matrix outcomes, including skips and failures |
| macOS compatibility | **Out of scope** | Not a target platform |
| DSM SPK packaging, DSM runtime, or TV/movie/music metadata API | **Not part of current implementation** | Historical plans only; no supported-runtime claim |

Mark a gate verified only after a specific command/test has passed at the final revision; record the exact environment and output. The source modules may be exercised independently of their CLI surface. Do not equate a green unit test with production readiness, or extrapolate Linux-host tests to Windows.

### Local development evidence (not a host qualification)

On 2026-11-01 in a Linux sandbox with Python **3.12.14** and an **uncommitted working tree**, `PYTHONPATH=src python3 -m ocd --help` exposed `watch`, `duplicates`, `artifact`, and `cache` alongside the organizer commands. Later integration validation reported **165 tests passed, 10 skipped** (including Windows-specific scenarios); rerun the full suite at the final commit before treating this figure as a release record. This checks the in-progress source snapshot only. It does not establish a reproducible clean-commit build, successful GitHub Actions matrix, a native Windows test, live network/cloud qualification, or production filesystem safety.

## Recommended verification levels

1. **Source check:** import and run `python -m ocd --help` without third-party runtime dependencies; run all repository tests and static security checks. Run the workflow matrix and retain its actual results.
2. **Read-only behavior:** exercise `init`, `roots add`, `roots list`, `scan`, `plan`, observe-only polling, and duplicate analysis against isolated fixture directories. Compare file bytes, paths, and directory entries before/after; cover hardlinks, unstable files, and error recovery.
3. **Verified local artifact handling:** exercise `artifact fetch --manifest ... --destination-root ... --artifact ...` without organizer registration; use controlled HTTPS and loopback fixtures with exact manifest size/hash, redirect and token boundaries, network interruptions, existing targets, and hostile destination paths. Verify `cache import` and `cache materialize` using independently computed hashes, missing/corrupt blobs, collisions, and recoverability tests. Qualify native Windows no-overwrite publication separately; a local download or local cache is not a cloud sync test.
4. **Controlled Linux apply:** operate only on disposable fixture files. Verify the required `apply` mode and `--yes`, `renameat2` availability, no replacements/deletion, source revalidation, target races, and journal recovery.
5. **Host qualification:** repeat supported read-only operations on named Windows and Linux filesystems, including permissions, case-sensitivity, symlinks/reparse points, mount boundaries, interruptions, and large trees. Windows apply must remain disabled until its native commit helper and surrounding journal/recovery behavior are separately qualified.
6. **Future feature qualification:** test any event-driven watcher, service installer, or cloud provider adapter independently. None inherits read-only polling or local artifact-download qualification.

Every qualification record should capture the exact commit, command, exit status, test fixture, platform and filesystem. Preserve negative results alongside positive results.

## Repository-startup security note

Past repository content included a concealed VS Code `folderOpen` task executing JavaScript disguised as `public/fonts/fa-solid-500.woff2`. The task and pseudo-font were removed; automatic workspace tasks were disabled with valid string `"off"`, and static/read-only checks were added for the identified path. Historic revisions can still contain the payload. Do not execute it. Repository-level checks cannot establish that an old checkout, host, editor extension, or user settings were never affected. This is a **separate security-history claim**, not evidence of organizer-runtime safety.
