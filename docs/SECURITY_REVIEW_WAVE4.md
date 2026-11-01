# OCD independent security review — wave 4

This is a **source-level adversarial review**, not a certification of a Windows
or production Linux installation. Scope: scanner, planner, apply/journal/locks,
duplicate hashing, manifest download, Win32 helper, CLI and monitoring. The
subject is a live, uncommitted working tree that other implementation agents
are modifying, so findings should be rechecked against the eventual exact
commit before merging.

## Material findings

1. **Important — Windows path-name aliases and visual spoofing.**
   `src/ocd/win32.py::_path_parts` rejects standard DOS devices but admits
   `CONIN$.txt`, `CONOUT$.txt` and bidirectional format controls such as U+202E.
   These are unsafe for a cross-platform component-name policy and can create
   different behavior between user-visible names and Win32 lookup semantics.
   `tests/test_security_wave4.py::test_win32_rename_rejects_console_aliases_and_bidi_names`
   reproduces all three accepted values. **Required:** tighten `_path_parts`
   and run this test plus the native Windows rename matrix before wiring the
   helper into apply. Status at initial test: **3 failures**.

2. **Important, resolved in concurrent implementation — artifact manifest
   path portability.** The initial `src/ocd/artifacts.py::_validate_relpath`
   accepted superscript-digit DOS device names (`COM¹`, `LPT²`), `CONIN$`,
   `CONOUT$`, Win32 forbidden characters (`< > ? * | "`) and bidi controls.
   Two portable-path regression tests were added. They **now pass** after the
   concurrent Windows-artifact implementation updated validation. Keep them
   in CI; do not infer that Windows publication is qualified from this alone.

3. **Important qualification gap — Windows mutation still requires native
   evidence.** `src/ocd/win32.py` mocks verify intended handle retention and
   `FileRenameInfo` arguments but cannot establish real filesystem behavior for
   concurrent hardlinks, junction swaps, path aliases, sharing violations,
   NTFS/ReFS variants, or source metadata/identity conversion. `apply.py`
   initially gates Windows moves; retain that gate unless host tests and the
   accompanying Windows journal/directory-creation boundary are verified.
   The original `src/ocd/apply.py::_open_journal` uses a pathname-based open
   with no `O_NOFOLLOW` equivalent on Windows; that must be part of native
   qualification if this branch is enabled.

4. **Important availability boundary — unbounded organizer scans.**
   `src/ocd/scanner.py::scan_root` collects all eligible files in memory and
   `src/ocd/monitor.py::watch_roots` repeats full scans indefinitely. There is
   no explicit entry/file/depth budget, unlike `scan_duplicates`. An extremely
   large or adversarial registered tree can exhaust memory or dominate I/O.
   This is an availability risk, not proof of unauthorized mutation. Add scan
   budgets, per-root backpressure and host performance measurements before
   unattended automatic monitoring.

## Adversarial controls exercised

| Boundary | Test | Observed source-level result |
|---|---|---|
| Atomic Linux rename to destination concurrently hardlinked to source | `test_destination_hardlink_inserted_at_commit_is_not_replaced` | **Pass**; neither source nor collision path lost |
| Linux source replaced with symlink to outside-root file before commit | `test_source_replaced_by_symlink_to_outside_at_commit_is_held` | **Pass**; outside content untouched, move held |
| Portable artifact manifest paths | Two `test_artifact_manifest_*` checks | **Pass** after concurrent patch |
| Windows rename path component parser | `test_win32_rename_rejects_console_aliases_and_bidi_names` | **3 failures** initially; fix required |

Existing `test_adversarial.py`, `test_apply.py`, `test_artifacts.py`,
`test_duplicates.py` and `test_win32.py` additionally cover symlink swaps,
destination collisions, interrupted journals, duplicate hardlink accounting,
remote redirect authentication stripping and mocked Win32 handles. These are
unit/regression controls; none substitute for destructive tests on disposable
NTFS/ReFS host fixtures and interrupted-process recovery.

## Merge advice

**Revise before merge** while the three path-parser failures are open. After
fixing them, rerun the full exact-commit test suite and independent review.
It is reasonable for Windows apply or artifact publication to remain explicitly
fail-closed if native qualification is absent; do not label source-only mocks
as production-supported Windows mutation. Cloud-provider uploads/sync and
autonomous model deletion remain outside the implementation reviewed here.

## No-data-loss boundary

All new tests use temporary fixture folders, static parsers and mock calls.
No real C:/D: paths, BT2 models, private cloud assets or workstation data were
read, modified or removed by this review.
