# OCD Chat Continuation — 2026-10-05 V1

Restore command:

`OCD::RESTORE_AND_RUN::DSM_GUI_ROUTE_FIX_20261005_V1`

## Authority / safety boundary

- Repository: `thebrazenbeard/ocd`.
- Do not merge `main`, tag, release, or claim live qualification without separate live-user authority.
- Use Project Runner for substantial execution work.
- Prefer Workbridge Commander on the user's Lappy when available for DSM/browser work.
- Preserve source/build/install/runtime/application states separately.
- Do not infer a live DSM PASS from CI or from an older installed package.

## Exact current source subject

Qualified OCD source:

`dfee676090d1539a49f4fc51b5881a370cbdd8e4`

This exact commit is on:

- `fix/dsm-cgi-route-0004-20261005`
- `feature/multi-root-install-20261005`
- `build/ocd-v1-sol-20261004`
- `build/ocd-v1-self-contained-spk-20261004`

Draft PR #1 remains the integration PR. `main` is intentionally untouched.

Vendored canonical packager:

`thebrazenbeard/spk-packager@09f1e1dd79a5f7b854131e6113319b2c25ecaa84`

The vendored packager source was previously proven byte-for-byte identical to that upstream subject.

## Product model now

OCD installation no longer requires a media root. The engine supports zero or many roots. Each root has an explicit media type (`tv`, `movie`, `music`) and independent Observe/Apply mode.

DSM service account is `sc-OCD`. OCD does not run as root and does not silently alter DSM shared-folder ACLs. A target DSM share must grant `sc-OCD` access before the root can be admitted.

The daemon administration/API server remains loopback-only at:

`http://127.0.0.1:9157`

The DSM-facing browser side is HTTPS through DSM (port 5001 on the live target). Do not confuse that HTTPS transport with the internal loopback HTTP hop.

## Installed live package state at checkpoint

The user's DS216 currently has OCD `0.1.0-0003` installed and running.

Live evidence for `0003`:

- DSM Package Center reports `0.1.0-0003`.
- Package state is Running.
- DSM exposes an OCD Open action / launcher.
- The DSM launcher/CGI itself is installed and executable.
- An unauthenticated request to the normalized lowercase third-party CGI path reached OCD's CGI and returned OCD's `403 DSM authentication required` guard. This proves the app/CGI is present and DSM executes it.
- Authenticated launcher use is NOT qualified: `0003` routes requests with `PATH_INFO`, and live browser use fell through that route handling and produced DSM's 404 page.

Therefore:

- DSM_INSTALLED(`0003`): PASS
- DSM_RUNNING(`0003`): PASS
- DSM_LAUNCHER_REGISTERED(`0003`): PASS
- DSM_GUI_BEHAVIOR(`0003`): FAIL
- APPLICATION_BEHAVIOR(`0003`): NOT QUALIFIED

## Important corrections discovered during live testing

1. DSM port 5001 is HTTPS. Browser-side DSM URLs must use/inherit HTTPS.
2. OCD port 9157 is intentionally HTTP because it is loopback-only inside the NAS.
3. The `app/` directory being inside `package.tgz` is NOT itself a defect. Synology's DSM package model expects `dsmuidir` to reference a directory in the package payload. Do not reintroduce the discarded outer-SPK theory.
4. DSM normalizes the third-party package UI path to lowercase `ocd` in the live target.
5. The actual `0003` defect is CGI route semantics: depending on `PATH_INFO` was not reliable in the live DSM webman path.

## 0.1.0-0004 repair

Exact source:

`dfee676090d1539a49f4fc51b5881a370cbdd8e4`

Key fixes:

- Package version advanced to `0.1.0-0004`.
- DSM app config launcher now uses the documented relative form:
  `3rdparty/ocd/index.cgi?path=/`
- CGI no longer reads `PATH_INFO`.
- CGI routes through an explicit allowlisted `QUERY_STRING` contract:
  `?path=/...`
- The embedded manager detects the lowercase DSM CGI path and maps API calls to:
  `/webman/3rdparty/ocd/index.cgi?path=<OCD API path>`
- Mutations still require the `X-OCD-DSM: 1` header.
- DSM session authentication still uses Synology's `authenticate.cgi`.
- DSM user must still be in `administrators`.
- Request body ceiling remains 1 MiB.
- Loopback daemon remains `127.0.0.1:9157`.

Regression tests explicitly require:

- version `0004`
- no `PATH_INFO` in the CGI
- `QUERY_STRING` / `path=` routing
- lowercase relative launcher path
- lower-case manager proxy URL
- package contract + CGI shell syntax

## Exact 0004 qualification

Local exact-commit qualification:

- package contract tests: PASS (8 tests)
- CGI shell syntax: PASS
- Go tests: PASS
- Go vet: PASS
- two independent Windows SPK builds: byte-identical
- strict SPK verify: PASS
- ARMv7 payload `e_machine=40`
- exact source provenance: PASS
- final worktree clean: PASS

Windows exact artifact:

`OCD-armada38x-0.1.0-0004.spk`

SHA-256:

`5a20ef886c698b0374af6a32bc33e3860260263576c96092f40f3e9d0a77a839`

Size:

`6,359,040` bytes

Binary SHA-256:

`91fec3e4d0aa6e3af95a7047b89cfe5ebd051a5ac6fe4bf40209f06ec36b2b74`

GitHub Actions exact-head run:

`37389672269`

All jobs PASS:

- Go 1.23.x
- Go 1.24.x
- Go 1.25.x
- SPK build

Exact CI artifact ID:

`11379229897`

Linux CI produced the exact same SPK SHA-256:

`5a20ef886c698b0374af6a32bc33e3860260263576c96092f40f3e9d0a77a839`

Therefore cross-host reproducibility for `0004` is PASS.

## Lappy artifact state

Before the last Workbridge disconnect, the exact CI-produced `0004` artifact had already been downloaded to Lappy and its SHA-256 was independently read back as:

`5a20ef886c698b0374af6a32bc33e3860260263576c96092f40f3e9d0a77a839`

Do not rebuild a different artifact for installation unless necessary. Prefer that exact CI artifact and re-check its hash before use.

## Workbridge / execution state at checkpoint

Workbridge Commander recovered and was used for the `0004` source/test/build flow and for DSM navigation. It disconnected again immediately before the final Package Center `0004` upgrade click sequence.

Do not assume the old Workbridge process IDs survive into the next chat. Open a fresh Workbridge shell and register a fresh Project Runner task.

SSH to the DS216 has been unreliable and should not be the primary installation path. The proven installation path is the already-authenticated DSM Package Center browser session using named UI controls.

## Browser automation facts already proven

Use named/accessibility controls; do not use blind coordinates.

The Firefox DSM window can be identified by an address bar containing the DSM host on port 5001.

Manual Install flow:

- Package Center -> Manual Install
- Browse
- native Windows `File Upload` dialog
- standard `File name` Edit control ID: `1148`
- standard `Open` Button control ID: `1`

This control-ID method successfully selected the exact `0003` artifact and is the correct pattern to adapt for `0004`.

DSM direct package-center launch can use DSM's own Package Center app identifier:

`SYNO.SDS.PkgManApp.Instance`

but prefer reading/invoking the live named Package Center control once the DSM desktop is loaded.

## Immediate next execution frontier

1. Reconnect Workbridge Commander on Lappy.
2. Open a fresh shell in the OCD worktree and register Project Runner work unit `DSM_GUI_ROUTE_FIX_V1` (or a clearly succeeding live-install unit).
3. Confirm canonical source branch/head is still `dfee676090d1539a49f4fc51b5881a370cbdd8e4`. If any branch moved, reconcile before acting.
4. Re-check the Lappy `0004` SPK hash equals:
   `5a20ef886c698b0374af6a32bc33e3860260263576c96092f40f3e9d0a77a839`
5. Open the existing authenticated DSM Package Center.
6. Manual-install/upgrade `0.1.0-0004` over installed `0003`.
7. Verify Package Center reports:
   - installed version `0.1.0-0004`
   - Running
   - Open action present
8. Click Open and verify the OCD manager actually renders (not DSM 404).
9. In the authenticated browser verify the CGI bridge:
   - root manager page via `?path=/`
   - health via `?path=/healthz` returns `ok`
   - status via `?path=/api/v1/status` returns JSON
10. Verify status reports exact source revision:
    `dfee676090d1539a49f4fc51b5881a370cbdd8e4`
11. Verify the manager shows/permits multiple independent roots.
12. Only after GUI/runtime readback succeeds, advance:
    - DSM_INSTALLED(`0004`)
    - DSM_RUNTIME_VERIFIED(`0004`)
    - DSM_GUI_BEHAVIOR(`0004`)
13. Then proceed to controlled multi-root Observe tests and later controlled Apply tests.
14. Update `docs/QUALIFICATION.md`, the continuation state, and Draft PR #1 with exact live evidence.
15. Keep `main` unmerged unless the live user separately authorizes merge.

## Current evidence state

For exact source/artifact `dfee676090d1539a49f4fc51b5881a370cbdd8e4` / `0.1.0-0004`:

- SOURCE_VALIDATED: PASS
- PACKAGE_CONTRACT: PASS
- SPK_STRUCTURALLY_VERIFIED: PASS
- LOCAL_REPRODUCIBLE: PASS
- CROSS_HOST_REPRODUCIBLE: PASS
- DSM_INSTALLED: PENDING
- DSM_RUNTIME_VERIFIED: PENDING
- DSM_GUI_BEHAVIOR: PENDING
- APPLICATION_BEHAVIOR_VERIFIED: PENDING
- RELEASE_PUBLISHED: PENDING

Do not promote any pending state by inference.

## Restore behavior

When the live user sends:

`OCD::RESTORE_AND_RUN::DSM_GUI_ROUTE_FIX_20261005_V1`

the new chat should:

1. read this continuation file first;
2. verify exact current Git refs/PR evidence;
3. reconnect Workbridge/Project Runner;
4. resume at the `0004` DSM upgrade and live GUI verification frontier;
5. not redo already-qualified source/build work unless the exact subject changed.
