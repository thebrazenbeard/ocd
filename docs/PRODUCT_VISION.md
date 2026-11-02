# OCD product vision and delivery gates

## Promise

**OCD (Organized & Clean Disk)** will be an installable Windows/Linux application that monitors user-tagged folders and organizes authorized **local and cloud storage spaces**. A user should be able to register where files arrive, define a reliable rule for where they belong, and let OCD maintain that arrangement—with visibility, conflict handling, recovery, and clear separation between *organize*, *offload*, and *delete*.

The README states this promise. Neither a one-off CLI script nor a read-only polling loop alone fulfills it.

The existing Python implementation is a **local developer-preview foundation**:

- Versioned local JSON configuration with explicitly registered roots.
- Local scanner and deterministic move planner with explicit folder-ancestor and extension tags.
- Observe-only foreground polling monitor, read-only SHA-256 duplicate reports, bounded config-free disk-usage reports, and an opt-in static offline HTML treemap; this is **not** a live GUI.
- An evidence-only `cleanup review` command: every large-file observation remains blocked from deletion and reclaimable bytes are unknown.
- A read-only `artifact verify` command for a previously downloaded file's pinned size and SHA-256.
- An optional bounded tagged-folder scanner API for future observers, not an installed durable monitor.
- A Windows/Linux wheel install smoke test in CI; native installers and background services remain unshipped.
- Deliberate Linux same-filesystem moves with source rechecks, no-overwrite behavior, journaling, and rollback attempts.
- Explicit manifest-based, size/hash-checked artifact **download** with source-level POSIX and experimental native Windows publication; Windows host qualification remains outstanding.
- Explicit content-addressed local artifact cache with verified import/materialization; not a cloud account, upload, or remote sync service.
- A Win32 no-replace rename helper under source-level tests, **not wired into enabled apply**.

Nothing above currently constitutes a Windows-installed service, automatic organizer, cloud sync client, or model artifact store.

## Core user journeys

### Set it up and trust it

1. Install OCD with a first-class Windows or Linux package and select a scoped, unprivileged operating identity.
2. Register one or more specific folders or provider locations; OCD shows exactly what its configured access covers.
3. Tag folders/rules for destinations. The preview has explicit folder-ancestor and extension rules; richer rules may include project identity, filename patterns, user labels, and validated content metadata.
4. Run a preview on existing contents. Show target paths, conflicts, file-settle status, and expected disk effects.
5. Authorize well-defined policies, including any unattended behavior, independently from registering a location.
6. Launch a persistent monitored service; watch its activity, pause it, inspect failures, and recover from interruptions.

### Keep local and cloud spaces organized

- Observe filesystem/provider changes without moving files while a writer is still updating them.
- Reconcile missed events after restart; polling snapshots remain a correctness backstop.
- Produce deterministic plans and a reusable operation journal.
- Apply only allowed cross-location operations with explicit semantics for staging, hashes, target collisions, retries, and eventual consistency.
- Never treat a copy as successful archival/offload until the destination's bytes, identity, metadata, and recoverability have been checked according to policy.
- When remote credentials are unavailable, expired, or scoped incorrectly, pause that location and report a precise hold; do not silently fall back to deleting or overwriting.

### Reclaim disk space deliberately

OCD should answer *what occupies space, why, and what can be safely removed?* Exact duplicate content and reclaim estimates are useful evidence, but they are not proof that a file is disposable. Reclaim workflows need live-use checks, ownership, hardlink/snapshot behavior, retention policy, backups, and a reversibility plan. Deleting user data is a separate high-risk, reviewable capability—not a side effect of a tag.

For model/training workflows: Git should retain source code, artifact manifests, run parameters, provenance, checksums, and download instructions. A model/artifact store should retain large base weights, adapters, final exports, and reproducibility evidence. VN1/VN2 workers should request verified artifacts on demand. Intermediate merged checkpoints may be reclaim candidates **only after** independently reconstructable or recoverable final outputs, adapters, base models, and receipts have been remotely verified. GitHub repository source storage is not the default large-weight archive. The current private BT2 GitHub releases contain uploaded assets, but **remain mutable drafts**; read-only workers cannot list draft releases. Publication within the still-private repository, authorized worker sign-in, independent byte verification, successful model load, and remote retention are separate gates before Lappy evicts its last fallback.

## Proposed architecture

| Layer | Responsibility | Required invariants |
| --- | --- | --- |
| Registry and policy | Explicit scopes, tags/rules, destinations, cloud accounts, retention, consent | Versioned migrations; no implicit wildcard authority; no secrets in exported config |
| Discovery and reconciliation | Windows/Linux file discovery and provider listings; filesystem events with periodic rescans | Detect symlinks/junctions; settle writes; bounded traversal; durable cursor/restart behavior |
| Planner | Classify and propose one operation per eligible file or object | Deterministic output; resolve identities/collisions; no actions on unknown inputs |
| Coordinator | Queue, retry, cancel, pause, rate-limit, and persist operation state | Idempotency, per-root mutual exclusion, cancellation safety, detailed audit trail |
| Local executor | Secure moves within or between volumes; local staging and verifies | No unauthorized overwrite; compare source identity; robust collision and rollback handling |
| Cloud adapters | Explicit provider-specific list/read/write/promote/delete APIs | Token scope isolation, object version/ETag checks, checksums, retries, resumable transfers |
| Model artifacts | Content-addressed manifests, verified download/upload, provenance | SHA-256 and byte count, immutable references, no unverified local eviction |
| Service + UI/CLI | Install/start/stop/status, rule editing, plan approval, reports | OS-native service supervision; observable failure; least-privilege operation |

Organizing a cloud location is not equivalent to downloading one HTTPS URL; treating them as one feature would hide the main missing architecture.

## Delivery milestones and acceptance criteria

### M0 — Preview integrity (current work)

- [x] Define explicit root registration and read-only scan/plan boundaries.
- [x] Add a polling observer and exact-content duplicate reporting without deletion.
- [x] Add a Linux-only explicit local move implementation, artifact retrieval, and a content-addressed local cache on POSIX; add experimental Windows download publication for native qualification.
- [x] Document Windows fail-closed state and add multi-platform source CI.
- [x] Ship read-only cleanup review with all deletion candidates blocked and no falsely precise reclaim estimate ([PR #6](https://github.com/thebrazenbeard/ocd/pull/6)).
- [x] Ship pinned existing-artifact size/SHA verification on Windows/Linux ([PR #7](https://github.com/thebrazenbeard/ocd/pull/7)).
- [x] Ship a static offline HTML disk-usage treemap, separate from the unfinished interactive GUI ([PR #9](https://github.com/thebrazenbeard/ocd/pull/9)).
- [x] Ship opt-in scanner budgets and fail-closed detected directory replacements, not durable observation ([PR #11](https://github.com/thebrazenbeard/ocd/pull/11)).
- [x] Validate the built Python wheel's installed CLI entry points in hosted Windows/Linux CI ([PR #12](https://github.com/thebrazenbeard/ocd/pull/12)).
- [x] Land the rebuilt developer-preview code and documentation; PR [#4](https://github.com/thebrazenbeard/ocd/pull/4) merged as `a4be396579649c4ce0d126004772f2846bd7c5f2` after passing Windows/Linux source CI and startup-safety checks.
- [ ] Complete reproducible native host qualification (especially NTFS/ReFS mutation and recovery), beyond the limited read-only `usage` smoke record.
- [ ] Rebuild the held draft [observer PR #10](https://github.com/thebrazenbeard/ocd/pull/10) as separately qualified state locking, durable event acknowledgement/replay, and foreground observation; its SQLite pathname/WAL/lock hazards preclude merging the current draft.
- [ ] Resolve security review findings and harden cancellation/recovery for hostile filesystems.

### M1 — Real, monitored Windows/Linux local organizer

- [ ] Define a stable tagging/policy model and migration path beyond today's folder-ancestor and extension rules.
- [ ] Qualify Windows native apply on actual NTFS/ReFS fixtures, including junction swap, hardlink collision, rename races, sharing violations, Unicode paths, and restart recovery.
- [ ] Qualify Linux local apply on supported filesystems, including rename/no-overwrite semantics and power-interruption recovery.
- [ ] Build background monitoring and missed-event reconciliation with no move loops.
- [ ] Package native Windows service and Linux user/system service; implement install, status, pause, resume, logs, and uninstall.
- [ ] End-to-end: an authorized tagged test file placed in a registered folder is organized automatically exactly once, with a durable record, on **both** target operating systems. Unregistered files remain untouched.

### M2 — Cloud organization and verified offloading

- [ ] Choose supported providers and define per-provider explicit read/write/deletion scopes.
- [ ] Support remote listing, conflict detection, version/ETag checks, resumable uploads/downloads, and checked promotion to stable names.
- [ ] Reconcile cloud changes across restarts and distinguish one-way offload from two-way synchronization.
- [ ] Store content-addressed artifacts and manifests with proven SHA-256/size verification and provenance.
- [ ] End-to-end: a permitted local artifact is copied to a remote object, independently verified, retrievable on a second worker, and only then eligible for a separate reviewed local eviction.
- [ ] Failure drills: interrupted transfer, stale token, remote collision, mismatched hash, intermittent network, rate limit, and partial rollback do not destroy the last valid copy.

### M3 — Storage intelligence and safe reclamation

- [ ] Account for hardlinks, snapshots, compression, sparse files, and live process use before reporting reclaim estimates.
- [ ] Cluster training runs by base model, adapter, export, hash, and provenance; distinguish independent experiments from backups.
- [ ] Produce reviewable, dry-run cleanup proposals with explicit retained artifacts, recovery references, estimated savings, and reversible staging.
- [ ] Require separate deletion authorization and an appropriate holding/retention period; never turn a duplicate report into silent removal.
- [ ] End-to-end: reclaim a disposable fixture only after backup-and-restore is independently validated, with audited bytes actually freed.

## Release and platform gates

No product-level label such as *installed*, *automatic*, *cloud-enabled*, *safe deletion*, or *Windows apply ready* should be used until its corresponding end-to-end acceptance test passes. The CI source matrix alone is not a substitute for tests on real Windows and Linux hosts with real filesystem semantics and host-policy/credential failures.

macOS is outside the product roadmap. Older DSM/SPK and media-metadata research in the repository is historical material, not an alternate runtime track.

See [Architecture](ARCHITECTURE.md) for what current source modules actually do and [Qualification](QUALIFICATION.md) for current test evidence and safety gaps.
