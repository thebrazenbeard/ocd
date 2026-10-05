# Qualification

No lower state implies a higher state. Qualification belongs to an exact source/artifact subject.

| State | Meaning | Current |
|---|---|---|
| SOURCE_VALIDATED | exact source compiles/tests and hostile checks pass | MUST MATCH CURRENT HEAD |
| SPK_STRUCTURALLY_VERIFIED | strict SPK verification passes for the exact artifact | MUST MATCH CURRENT HEAD |
| SPK_REPRODUCIBLE | independent qualified builds are byte-identical | MUST MATCH CURRENT HEAD |
| DSM_INSTALLED | exact qualified SPK installed on a named DSM subject | PENDING |
| DSM_RUNTIME_VERIFIED | DSM unit, process, API, package identity verified live | PENDING |
| APPLICATION_BEHAVIOR_VERIFIED | observe/apply behavior verified on controlled media fixtures/live root | PENDING |

## Historical package evidence

Implementation subject `21b2919134daa4ea45f57df8e25561c33eb97e6b` was an earlier structurally verified and locally reproducible package subject. Its hashes and PASS states are historical only; later DSM-account, lifecycle-bootstrap, and cross-host-reproducibility fixes supersede it for installation.

Historical evidence for that subject included:

- `go test ./...`: PASS;
- `go vet ./...`: PASS;
- SPK Packager lint and strict verify: PASS;
- deterministic double-SPK build: PASS;
- ARMv7 payload with ELF `e_machine=40`.

Do not reuse an older SPK hash, CI result, or provenance record for a newer commit.

## Current-head rule

A candidate may advance SOURCE/SPK states only when the evidence names the exact current Git commit and artifact. Windows/Linux reproducibility requires complete SPK byte identity, not merely an identical Go payload. Generated provenance therefore uses explicit UTF-8/LF bytes.

DSM installation is not attempted until the exact current artifact has passed source tests, strict package verification, reproducibility checks, and lifecycle review. DSM runtime/application states remain open until read back from the NAS.

The user's DS216 was upgraded through `0.1.0-0003`. DSM reports `0003` installed and running and exposes the OCD Open action, but live launcher qualification FAILS for that subject: authenticated requests to the packaged CGI reach a route mismatch and DSM renders a 404. The repair is versioned `0.1.0-0004`, which replaces the CGI's `PATH_INFO` routing with an explicit `?path=` contract and requires its own exact-artifact install/runtime readback.

See `docs/CONTINUATION_20261004_V1.md` for durable continuation context.
