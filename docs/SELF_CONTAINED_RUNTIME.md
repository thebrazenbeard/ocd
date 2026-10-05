# Self-contained runtime and semantic authority

OCD has one semantic authority: this repository.

The repository owns parsing, matching, ambiguity handling, naming policy, safety gates,
mutation planning, collision prevention, sidecar handling, journaling, watcher behavior,
reconciliation, configuration semantics, and service/API behavior. The installed SPK is a
compiled deployment of those rules, not a second reasoning system.

External services are data inputs only:

- TVmaze may supply television metadata.
- TMDB may supply movie metadata when the user configures a token.
- Embedded media tags supply v0.1 music metadata.
- Future databases or metadata providers may enrich facts, but they must not own OCD's
  policy, confidence/ambiguity rules, mutation authority, or naming semantics.

The running package has no BT2, Project Runner, companion-repository, remote-agent, or
database dependency for its control logic. Loss of every external metadata provider may
hold metadata-dependent files for review, but it must not remove OCD's own decision logic,
state model, safety checks, service lifecycle, or ability to explain why an action was or
was not allowed.

## Build containment

The repository contains:

- OCD application source;
- vendored Go dependencies under `vendor/`;
- the qualified SPK packaging/lint/verification implementation under
  `tools/spk_packager/`;
- DSM package source under `package-source/`;
- repository-local Linux and Windows build entrypoints.

A build environment still needs the Go and Python toolchains, Git, and ordinary operating
system utilities. Those are toolchains, not semantic/runtime dependencies. SPK builds pin
Go 1.23.12 so the compiled payload is reproducible across qualified build environments;
source-compatibility tests may run under newer Go versions.

## Source binding

Every provenance-bearing SPK build must come from a clean tracked Git working tree.
The build embeds the exact 40-hex commit in the OCD binary, `/api/v1/status`, and
`SOURCE_PROVENANCE.json` inside the SPK. The provenance payload also records the pinned Go
compiler version used to produce the binary.

The source repository is `https://github.com/thebrazenbeard/ocd`, and the runtime exposes
a direct commit URL. A build from uncommitted tracked source fails closed rather than
claiming a false source identity.

## Distribution

Tagged releases build the SPK from this repository and attach it to the corresponding
GitHub Release together with `SHA256SUMS`. CI artifacts may still be used for branch
qualification, but a release asset is the durable user download surface.
