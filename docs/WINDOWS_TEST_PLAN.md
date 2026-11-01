# Windows native qualification plan (disposable fixtures only)

This is a **test plan, not a certification**. OCD targets Windows and Linux,
but the Windows filesystem-mutating path is not yet approved for real data.
Keep `apply_moves` fail-closed on Windows until the complete move, journal,
locking, rollback, and recovery path passes the gates below. Never use C:/D:
training directories, BT2 models, Docker data, Downloads, or any other real
user content as a qualification fixture.

## Scope and isolation

- Use the current **WorkBridge Commander** route to Lappy, not the retired
  `lappy-operator` endpoint. Run commands in an ordinary PowerShell process;
  do not open historical repository revisions in VS Code. Do not elevate merely
  to make an unsupported test pass.
- Work from an exact Git commit that has been pushed and reviewed. A local
  uncommitted workspace cannot establish reproducible native results.
- `tests/test_artifacts_win32.py` and `tests/test_win32_apply.py` create their
  own `tempfile.TemporaryDirectory` fixtures and use a loopback HTTP server for
  download tests. They do not need or touch real model files. The temporary
  directory is normally under `%TEMP%` on C:. If testing D: or ReFS, create a
  **new uniquely named fixture root** and arrange an explicitly scoped variant;
  do not change drive-wide settings or relocate a production directory.
- Record OS build, Python version, NTFS/ReFS volume, commit SHA, command,
  exit code, skip reason, and salient output for each test. A skip is **not**
  success. Stop on an unexpected write, an unauthorized network request, or
  path escape.

### Safe initial PowerShell session

The following is a template for execution by WorkBridge Commander **after the
rebuild branch is pushed**. It only clones source into a newly created temp
directory and runs fixture tests. Change the branch name if the approved commit
is on another branch. Do not use a preexisting directory as `$qualRoot`.

```powershell
$qualRoot = Join-Path $env:TEMP ("ocd-qualification-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $qualRoot -ErrorAction Stop | Out-Null
git clone --depth 1 --branch rebuild/windows-linux-ocd-20261101 https://github.com/thebrazenbeard/ocd.git (Join-Path $qualRoot 'source')
Set-Location (Join-Path $qualRoot 'source')
git rev-parse HEAD
[System.Environment]::OSVersion.VersionString
py -3 --version
Get-Volume -DriveLetter C | Select-Object DriveLetter,FileSystem,HealthStatus
$env:PYTHONPATH = 'src'
py -3 -m unittest discover -s tests -v
py -3 -m unittest -v tests.test_artifacts_win32 tests.test_win32_apply tests.test_win32
```

If `py` is unavailable, record that prerequisite failure; do not install
software or change system settings without separate permission. Keep the
fixture checkout for debugging until reviewed. Remove only the exact
`$qualRoot` created by this session after reviewing results.

## Native gate matrix

| Gate | Disposable probe | Required result |
|---|---|---|
| Fixed local drive and filesystem | Query `Get-Volume` and Win32 `GetDriveTypeW`/`QueryDosDeviceW` using fixture drive | Fixed, unmapped NTFS or ReFS. Unsupported, SUBST, UNC and removable paths fail closed. NTFS pass does not certify ReFS. |
| Artifact success | `test_publishes_verified_new_file_atomically` via loopback server | Exact SHA-256 and length; target visible only after complete verified write; no residual stage. |
| Artifact digest failure | `test_bad_digest_leaves_no_published_or_staging_file` | No published target and no leftover staging object. |
| Artifact collision | Re-fetch same target and create pre-existing different target | Existing bytes unchanged; no replacement, no renamed-over hardlink. |
| Win32 rename | `test_rename_disposable_fixture_to_unoccupied_target` | Source gone, destination bytes identical, correct Unicode filename, same volume only. |
| Existing distinct target | `test_existing_different_file_is_never_replaced` | Raise, source intact, existing target intact. |
| Existing same-file hardlink | `test_existing_same_file_hardlink_is_not_consumed` | Raise, both names/links intact; an unexpected success is a **blocker**. |
| Reparse parent | `test_reparse_destination_parent_is_refused` | Raise, no file appears outside fixture. Skips due Developer Mode/privilege require follow-up with a safely configured test host. |
| Windows public apply gate | `test_apply_does_not_write_journal_or_move` | Until full transaction qualification: hold, zero journal writes, zero moves/dirs. |
| Observe mode | `test_observe_mode_is_unchanged` | Files stay identical, no journal and no destination creation. |
| Dangerous names | `tests.test_security_wave4` and `tests.test_win32` | Device aliases, trailing dots/spaces, ADS/punctuation, Unicode bidi/surrogates refused; legitimate Unicode accepted. |

## Additional adversarial tests required before enabling Windows apply

These cases are **not fully covered by the current fixture suite**. Implement
bounded, deterministic tests with instrumentation or synchronization barriers,
then run them on both the supported Windows build and each supported volume
type. Never substitute random stress alone for a reproducible race test.

1. **Concurrent hardlink creation:** create a hardlink at the destination
   between the initial no-target probe and the native `FileRenameInfo` call.
   The source and destination must both survive intact and no link may be
   consumed, including on NTFS and ReFS. Test a destination that's already a
   link to the same opened source file, not only an unrelated file.
2. **Junction/symlink swap:** synchronize replacement of source or destination
   ancestor with a junction to another *fixture-owned* directory; assert no
   write occurs outside the selected fixture root, and the operation fails
   closed. Test dangling links and nested reparse components. Never point an
   attack fixture at personal or system data.
3. **Identity and consistency:** compare CPython `st_dev`/`st_ino` with the
   Win32 full `FILE_ID_INFO` identity on the installed Python version; cover
   changed size/mtime, changed source identity, source hardlinks, read-only
   files, sharing violations, Unicode normalization, drive-letter case, and
   case-insensitive target collisions.
4. **Journal and crash recovery:** test exclusive interprocess locks,
   append+flush durability, power/interruption boundary after intent and
   after rename, unreadable/corrupted/incomplete journal, undo rollback,
   rollback collision and manual-recovery holds. A native rename helper alone
   does **not** qualify the public transaction: `_open_journal`, parent
   creation, parent durability and rollback need a Windows-safe redesign.
5. **Artifact path attacks:** exercise stage-file hardlink and race attempts,
   pre-existing destination, target/ancestor reparse swaps, blocked cleanup,
   failed stream/size mismatch, interrupted download, redirect auth stripping,
   and restoration. Assert no partial target appears and no existing bytes
   change. Tests must use a local server or approved controlled HTTPS host.
6. **Read-only organizer behavior:** register a newly created fixture source
   and destination; compare full directory listings and hashes before/after
   `scan`, `plan`, `watch`, `duplicates`, and `roots list`. Include nested
   `folder:` tags, extension fallback, unsettled files, 1000+ entries,
   inaccessible paths, and symlink/junctions. Observation must not mutate.

## Release gate

- **Read-only Windows preview:** may be called qualified only after complete
  CLI/fixture native results with no unexplained skips; record named machine,
  OS build, Python, commit, filesystem, command and result.
- **Windows artifact fetch:** may be advertised for a supported Windows
  filesystem only after native success, collision, checksum, cleanup,
  authorization, and adversarial race gates are green; otherwise state
  source-implemented/unqualified or fail-closed.
- **Windows apply:** remains **disabled** until every move, journal,
  concurrent hardlink, junction swap, rollback, recovery, and interruption
  gate is demonstrably green, including the public command and no-overwrite
  contract. Do not bypass the gate to run exploratory commands on real data.
- **Linux:** separate native distro/filesystem qualification; Windows results
  cannot qualify Linux and vice versa.
- **Cloud remote sync/upload:** beyond local download and content-addressed
  staging; needs independent provider credential, retry, version, integrity,
  deletion and remote-conflict tests before any production claim.

Record the completed evidence and residual blocked gates in
`docs/QUALIFICATION.md` and attach a specific CI run/commit, rather than
replacing a skipped or source-only result with a green badge.
