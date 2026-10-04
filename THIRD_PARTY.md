# Third-party components

OCD directly depends on:

- `github.com/fsnotify/fsnotify` — filesystem notification abstraction used for Linux inotify watches.
- `github.com/dhowden/tag` — pure-Go embedded audio metadata reader.

Indirect Go module dependencies are pinned in `go.mod` / `go.sum`.

No Sonarr, Radarr, Plex organizer, SynoCommunity, or SPK Packager implementation is vendored into OCD. Those projects informed architecture and packaging decisions only.
