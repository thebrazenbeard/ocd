# OCD architecture v0.1

## Root contract

A watched root is durable configuration:

```
absolute path
+ media type
+ metadata provider
+ mutation mode
```

Media type is mandatory: `tv`, `movie`, or `music`.

OCD intentionally refuses mixed-type roots and overlapping/nested registered roots.

## Format contract

### Television

Target:

```
<root>/<Show>/Season <NN>/S<NN>E<NN> - <Episode Name>.<ext>
```

Input must expose an `SxxExx` episode token in v0.1. TVmaze resolves the canonical show and episode names. Specials use Season 00 when source numbering says season 0.

### Movies

Target:

```
<root>/<Title> (<Year>)/<Title> (<Year>).<ext>
```

TMDB resolves the canonical title and release year. A TMDB API Read Access Token is required. If credentials or a confident unique match are absent, OCD holds the file.

### Music

Target:

```
<root>/<Artist>/<Album>/<Song Title>.<ext>
```

Embedded Artist, Album, and Title tags are the source of truth in v0.1. Track number, disc number, and year stay inside the audio metadata and are not added to filenames. OCD does not rewrite media tags in v0.1.

## Runtime pipeline

1. **Root admission**
   - absolute/canonical path;
   - explicit media type;
   - provider/type compatibility;
   - readable/listable as the OCD package user;
   - apply roots must pass a create/remove probe.

2. **Watch**
   - recursive fsnotify/inotify directory watches;
   - newly created directories are added;
   - noisy file events are debounced;
   - periodic reconciliation covers missed events and restarts.

3. **Stability**
   - regular supported media file;
   - mtime older than settle interval;
   - watcher path gets an additional size/mtime comparison before processing.

4. **Parse and resolve**
   - type-specific parser;
   - type-specific provider;
   - no cross-type guessing.

5. **Plan**
   - deterministic target path;
   - target must remain in the same registered root;
   - collision means HOLD;
   - matching video sidecars are included.

6. **Apply**
   - observe roots only record the plan;
   - apply roots append journal intent first;
   - same-filesystem `rename(2)` semantics only;
   - no overwrite and no copy/delete fallback;
   - partial operation batches attempt reverse-order rollback.

7. **Suppress**
   - source and target paths changed by OCD are suppressed briefly from watcher reprocessing.

## State

Package state is small and reconstructible:

- `config.json` — typed roots, settle/reconcile settings, TMDB credential;
- `journal.jsonl` — mutation intents/completions;
- in-memory recent plans/findings — operational UI state.

No database is required for v0.1.

## HTTP boundary

The API binds only to loopback:

- `GET /healthz`
- `GET /api/v1/status`
- `GET|POST /api/v1/roots`
- `DELETE /api/v1/roots/{id}`
- `GET /api/v1/plans`
- `GET /api/v1/findings`
- `PUT /api/v1/settings/tmdb`
- `POST /api/v1/reconcile`

Loopback binding is a security boundary for v0.1, not an inconvenience to route around silently. A future DSM-authenticated portal can replace it.
