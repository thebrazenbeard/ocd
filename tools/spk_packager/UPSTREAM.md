# Vendored SPK packager provenance

The `spk_packager/` package in this directory is vendored into OCD so a checkout of
`thebrazenbeard/ocd` contains the DSM packaging, lint, and verification logic it uses.

Vendored source subject:

- repository: `thebrazenbeard/spk-packager`
- commit: `dbda5bca7a18d919346f52d8401a00c6fd530b88`
- upstream version: `0.1.0`

OCD intentionally does **not** import or clone that repository during its normal build.
The source subject above records provenance for the vendored code; the OCD repository is
the build authority for OCD releases.
