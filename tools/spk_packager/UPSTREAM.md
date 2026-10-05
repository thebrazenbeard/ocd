# Vendored SPK packager provenance

The `spk_packager/` package in this directory is vendored into OCD so a checkout of
`thebrazenbeard/ocd` contains the DSM packaging, lint, and verification logic it uses.

Vendored source subject:

- repository: `thebrazenbeard/spk-packager`
- commit: `09f1e1dd79a5f7b854131e6113319b2c25ecaa84`
- upstream version: `0.1.0`

OCD intentionally does **not** import or clone that repository during its normal build.
The source subject above records provenance for the vendored code; the OCD repository is
the build authority for OCD releases.

The vendored `spk_packager/` directory is intended to match upstream `src/spk_packager/`
byte-for-byte at the pinned commit. OCD-specific packaging policy belongs in OCD's manifest,
build scripts, and package contract tests rather than in an undocumented fork of the packager.
