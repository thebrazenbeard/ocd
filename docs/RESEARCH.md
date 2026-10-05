# OCD research record

Research date: 2026-10-04.

## Filesystem watching

OCD uses `fsnotify`, which maps to Linux inotify. Important constraints admitted into the architecture:

- directory trees are not watched recursively by a single inotify watch; OCD registers each directory;
- new directories must be added after creation;
- watch limits can be reached on large trees;
- network filesystems may not provide reliable notifications;
- a periodic reconciliation scan is therefore retained even when inotify is healthy.

Reference: `fsnotify/fsnotify`.

## Television metadata

TVmaze's public API requires no authentication for ordinary show/episode metadata. The API supports fuzzy show search and exact episode lookup by show id + season + episode number.

OCD uses the search endpoint first instead of single-search because TVmaze explicitly warns that identical show names are ambiguous under single-search.

Reference: https://www.tvmaze.com/api

## Movie metadata

TMDB's API requires application authentication using an API key or API Read Access Token. OCD uses the Bearer-token form and the movie search endpoint.

Credentials are user supplied and stored in the package's private state directory.

Reference:
- https://developer.themoviedb.org/docs/authentication-application
- https://developer.themoviedb.org/docs/search-and-query-for-details

## Music metadata

Music filenames intentionally do not carry track/disc numbers under the OCD contract. Embedded tags are primary in v0.1, using the pure-Go `dhowden/tag` reader.

MusicBrainz is the intended future enrichment/repair provider. Its public API currently requires a meaningful User-Agent and normally limits clients to at most one request per second. OCD does not need remote MusicBrainz calls when Artist/Album/Title tags are already present.

Reference: https://musicbrainz.org/doc/MusicBrainz_API

## Organizer references

Sonarr/Radarr and smaller Plex organizer repositories were reviewed for mechanism rather than copied:

- separate type-specific naming logic;
- move/rename only after metadata resolution;
- collision handling;
- keeping file organization distinct from metadata lookup.

OCD deliberately does **not** import their broader download-client/indexer/library-management scope.

## DSM package permissions

DSM 7 packages are expected to run as non-root internal package users. SynoCommunity's current DSM 7 packaging convention names the effective account `sc-<package>`; for OCD that is `sc-OCD`. Shared-folder access is a separate ACL/resource concern.

Synology's `data-share` resource worker can grant read/write permission to an internal package user and create package-local share links. OCD previously used that mechanism for a single install-wizard root.

That design was intentionally removed in package revision `0.1.0-0002`: installation now creates no media root and requests no media-share resource. OCD starts with zero roots, and users may register any number of explicitly typed roots afterward. Each target DSM shared folder must grant the internal service account `sc-OCD` the required ACL. OCD validates access on root admission rather than running as root or silently changing DSM permissions.

Synology supports DSM desktop/package launch integration through `dsmuidir` and `dsmappname`. Synology's application-authentication guidance explicitly recommends calling `/usr/syno/synoman/webman/modules/authenticate.cgi` from a package CGI so the inherited DSM request environment can validate the current login. OCD uses that pattern instead of exposing its mutation API directly to the LAN: DSM links the packaged `app/` directory under `/webman/3rdparty/OCD`, an admin-only launcher opens `index.cgi`, and the CGI authenticates the DSM user, verifies `administrators` membership, then proxies only allowlisted OCD paths/methods to `127.0.0.1:9157`.

References:
- Synology Developer Guide: Resource / Data Share
- `SynoCommunity/spksrc` DSM 7 permission, service-account, deterministic packaging, and resource conventions
- `SynoCommunity/spkrepo` and `jdel/sspks` for Package Center repository/feed behavior (distribution layer only)
- `john-shine/synology-baiduNetdisk-package` as a legacy hand-built SPK comparison; its DSM 7 limitations are not adopted
- `thebrazenbeard/spk-packager` qualified DSM packaging framework
