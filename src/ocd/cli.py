"""Command-line entry points with explicit, fail-closed mutation consent."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

from .config import (
    ConfigError, add_root, default_config_path, init_config, load_config,
    save_config, verified_root,
)
from .types import RootConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ocd", description="Safe, opt-in file organization")
    parser.add_argument("--config", type=Path, default=None, help="JSON config location (default: per-user config)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create an empty, observe-by-default configuration")

    roots = commands.add_parser("roots", help="manage explicitly allowed roots")
    roots_commands = roots.add_subparsers(dest="roots_command", required=True)
    add = roots_commands.add_parser("add", help="register existing source and destination directories")
    add.add_argument("source", type=Path)
    add.add_argument("destination", type=Path)
    add.add_argument("--mode", choices=("observe", "apply"), default="observe")
    add.add_argument("--tag", action="append", default=[], metavar="TAG=LABEL",
                     help="folder or extension routing: folder:Invoices=Accounting or .jpg=Photos")
    add.add_argument("--settle-seconds", type=float, default=2.0)
    roots_commands.add_parser("list", help="list registered roots")

    for name in ("scan", "plan"):
        command = commands.add_parser(name, help=f"{name} only registered roots; never change files")
        command.add_argument("--root", type=Path, help="registered source path (default: all roots)")

    watch = commands.add_parser("watch", help="continuously observe registered roots; never change files")
    watch.add_argument("--root", type=Path, help="registered source path (default: all roots)")
    watch.add_argument("--interval", type=float, default=1.0, metavar="SECONDS",
                       help="positive polling interval in seconds (default: 1)")

    duplicates = commands.add_parser(
        "duplicates", help="find exact-content duplicate candidates; never change or delete files"
    )
    duplicates.add_argument("--root", type=Path, help="registered source path (default: all roots)")
    duplicates.add_argument("--max-files", type=int, default=100_000,
                            help="maximum eligible files to inspect (default: 100000)")
    duplicates.add_argument("--max-entries", type=int, default=200_000,
                            help="maximum filesystem entries visited (default: 200000)")

    usage = commands.add_parser(
        "usage", help="bounded read-only disk usage by explicitly chosen directory; no cleanup"
    )
    usage.add_argument("--path", type=Path, required=True,
                       help="absolute existing directory or drive root to measure")
    usage.add_argument("--max-entries", type=int, default=100_000,
                       help="maximum entries processed (one lookahead detects overflow; default: 100000)")
    usage.add_argument("--max-depth", type=int, default=32,
                       help="maximum directory depth from root (default: 32)")
    usage.add_argument("--top", type=int, default=20,
                       help="maximum largest files and top-level groups to show (default: 20)")
    formats = usage.add_mutually_exclusive_group()
    formats.add_argument("--json", action="store_true",
                         help="print a machine-readable report, including completeness")
    formats.add_argument("--html", type=Path, metavar="ABSOLUTE_OUTPUT_FILE",
                         help="create a self-contained, no-overwrite HTML treemap outside the scan root")

    cleanup = commands.add_parser(
        "cleanup", help="review large-file observations; never delete or claim reclaimable bytes"
    )
    cleanup_commands = cleanup.add_subparsers(dest="cleanup_command", required=True)
    review = cleanup_commands.add_parser(
        "review", help="read-only large-file review; all deletion decisions are blocked"
    )
    review.add_argument("--path", type=Path, required=True,
                        help="absolute existing directory to review")
    review.add_argument("--max-entries", type=int, default=100_000)
    review.add_argument("--max-depth", type=int, default=32)
    review.add_argument("--top", type=int, default=20)
    review.add_argument("--json", action="store_true", help="machine-readable blocked observations")

    apply = commands.add_parser("apply", help="replan and execute allowed moves (never delete)")
    apply.add_argument("--root", type=Path, required=True, help="registered source path")
    apply.add_argument("--yes", action="store_true", help="required consent to execute moves")

    artifact = commands.add_parser("artifact", help="explicit manifest-verified artifact downloads; no sync")
    artifact_commands = artifact.add_subparsers(dest="artifact_command", required=True)
    fetch = artifact_commands.add_parser("fetch", help="download exactly one named artifact without overwrite")
    fetch.add_argument("--manifest", type=Path, required=True, help="local JSON artifact manifest")
    fetch.add_argument("--destination-root", type=Path, required=True,
                       help="existing absolute local download directory")
    fetch.add_argument("--artifact", required=True, metavar="DESTINATION_RELPATH",
                       help="exact destination_relpath of the manifest entry to fetch")
    verify = artifact_commands.add_parser(
        "verify", help="read-only size/SHA-256 check of one existing manifested local file"
    )
    verify.add_argument("--manifest", type=Path, required=True,
                        help="local JSON artifact manifest")
    verify.add_argument("--destination-root", type=Path, required=True,
                        help="existing absolute local directory to inspect without writes")
    verify.add_argument("--artifact", required=True, metavar="DESTINATION_RELPATH",
                        help="exact destination_relpath of the manifest entry to verify")
    verify.add_argument("--json", action="store_true", help="machine-readable verification result")

    cache = commands.add_parser("cache", help="explicit SHA-256 content-addressed local artifact cache; no sync or deletion")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_import = cache_commands.add_parser("import", help="verify and copy a selected file into a local cache")
    cache_import.add_argument("--store-root", type=Path, required=True,
                              help="existing absolute SHA-256 cache root")
    cache_import.add_argument("--source", type=Path, required=True,
                              help="absolute path to the file whose bytes will be verified")
    cache_materialize = cache_commands.add_parser(
        "materialize", help="verify and stage one cached artifact for a running job, never overwrite"
    )
    cache_materialize.add_argument("--store-root", type=Path, required=True,
                                   help="existing absolute SHA-256 cache root")
    cache_materialize.add_argument("--destination-root", type=Path, required=True,
                                   help="existing absolute job staging root")
    cache_materialize.add_argument("--destination", required=True, metavar="RELPATH",
                                   help="portable relative job-staging path")
    for command in (cache_import, cache_materialize):
        command.add_argument("--sha256", required=True, metavar="HEX",
                             help="64-character expected SHA-256 digest (never inferred)")
        command.add_argument("--size", type=int, required=True, metavar="BYTES",
                             help="exact expected file size in bytes")
    return parser


def _tags(entries: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ConfigError(f"invalid --tag {entry!r}; use folder:SUBFOLDER=LABEL or .EXT=LABEL")
        tag, label = entry.split("=", 1)
        if tag in result:
            raise ConfigError(f"duplicate --tag rule: {tag}")
        result[tag] = label
    return result


def _select_roots(roots: tuple[RootConfig, ...], selected: Path | None,
                  *, verify: bool = True) -> tuple[RootConfig, ...]:
    if not roots:
        raise ConfigError("no registered roots; use 'ocd roots add' first")
    if selected is None:
        return tuple(verified_root(root) for root in roots) if verify else roots
    if not selected.is_absolute():
        raise ConfigError("--root must be an absolute registered source path")
    # Compare before resolving: --root is an allowlist key, not an arbitrary path
    # or symlink whose target could be switched between selection and use.
    matching = tuple(root for root in roots if selected == root.source)
    if len(matching) != 1:
        raise ConfigError(f"source is not an explicitly registered root: {selected}")
    return (verified_root(matching[0]),) if verify else matching


def _scan(root: RootConfig):
    # Deferred imports keep CLI help/config commands free of scanner side effects.
    from .scanner import scan_root

    return scan_root(root)


def _plan(root: RootConfig):
    from .planner import build_plan

    return build_plan(root, _scan(root))


def _display_plan(root: RootConfig, plan) -> None:
    print(f"Root: {root.source} -> {root.destination} [{root.mode}]")
    for move in plan.moves:
        print(f"MOVE {move.source} -> {move.destination}")
    for hold in plan.holds:
        print(f"HOLD {hold.path}: {hold.reason}")
    print(f"{len(plan.moves)} move(s), {len(plan.holds)} hold(s); no changes made")


def _display_watch_event(event) -> None:
    if event.kind == "error":
        print(f"WATCH ERROR {event.root.source}: {event.error}; no changes made", file=sys.stderr)
        return
    plan = event.plan
    assert plan is not None
    print(f"WATCH {event.root.source} -> {event.root.destination} [observe-only]")
    for move in plan.moves:
        print(f"MOVE {move.source} -> {move.destination}")
    for hold in plan.holds:
        print(f"HOLD {hold.path}: {hold.reason}")
    print(f"{len(event.files)} file(s), {len(plan.moves)} planned move(s), "
          f"{len(plan.holds)} hold(s); no changes made")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        path = (args.config if args.config is not None else default_config_path()).expanduser().resolve()
        if args.command == "init":
            init_config(path)
            print(f"Created {path}; no roots registered (observe-only default)")
            return 0

        if args.command == "artifact":
            from .artifacts import ArtifactError, fetch_verified, load_manifest

            if args.artifact_command == "fetch":
                # Keep the existing fetch error/status contract unchanged:
                # invalid manifests/selections are CLI input errors (2), while
                # a selected download failing verification is operational (1).
                entries = load_manifest(args.manifest)
                matches = tuple(entry for entry in entries if entry.destination_relpath == args.artifact)
                if len(matches) != 1:
                    raise ConfigError(f"artifact {args.artifact!r} is not present in manifest")
                try:
                    downloaded = fetch_verified(matches[0], args.destination_root)
                except ArtifactError as exc:
                    print(f"ocd: artifact fetch failed: {exc}", file=sys.stderr)
                    return 1
                print(f"Fetched and SHA-256 verified {downloaded}; existing files never overwritten")
                return 0

            try:
                entries = load_manifest(args.manifest)
                matches = tuple(entry for entry in entries if entry.destination_relpath == args.artifact)
                if len(matches) != 1:
                    raise ArtifactError(f"artifact {args.artifact!r} is not present in manifest")
                from .verification import verify_local_artifact

                verified = verify_local_artifact(matches[0], args.destination_root)
                if args.json:
                    import json

                    print(json.dumps({
                        "artifact": args.artifact,
                        "path": str(verified.path),
                        "size": verified.size,
                        "sha256": verified.sha256,
                        "verified": True,
                        "deletion_eligible": False,
                        "reclaimable_bytes": None,
                    }, sort_keys=True))
                else:
                    print(f"Verified existing artifact {verified.path}: "
                          f"{verified.size} bytes, SHA-256 {verified.sha256}; "
                          "read-only, no eviction authorized")
                return 0
            except ArtifactError as exc:
                if args.json:
                    import json

                    print(json.dumps({
                        "artifact": args.artifact,
                        "verified": False,
                        "deletion_eligible": False,
                        "reclaimable_bytes": None,
                        "error": str(exc),
                    }, sort_keys=True))
                else:
                    print(f"ocd: artifact verify failed: {exc}", file=sys.stderr)
                return 1

        if args.command == "cache":
            # Cache operations are stand-alone, user-selected storage actions;
            # they never create or read organizer configuration, watch a root,
            # apply a plan, or delete the source artifact.
            from .cloud import BlobRef, CloudStoreError, LocalContentStore

            try:
                identity = BlobRef(args.sha256, args.size)
            except CloudStoreError as exc:
                raise ConfigError(str(exc)) from None
            try:
                store = LocalContentStore(args.store_root)
                if args.cache_command == "import":
                    output = store.import_file(args.source, identity)
                    print(f"Imported and SHA-256 verified {output}; source preserved at {args.source}")
                else:
                    output = store.materialize(identity, args.destination_root, args.destination)
                    print(f"Materialized and SHA-256 verified {output}; cache preserved, no overwrite")
            except CloudStoreError as exc:
                print(f"ocd: cache {args.cache_command} failed: {exc}", file=sys.stderr)
                return 1
            return 0

        if args.command == "usage":
            # Standalone explicit read-only discovery; never loads organizer
            # config, invokes apply, or treats logical bytes as reclaimable.
            from .usage import scan_usage

            report = scan_usage(
                args.path, max_entries=args.max_entries,
                top=args.top, max_depth=args.max_depth,
            )
            if args.html is not None:
                from .treemap import write_usage_html

                write_usage_html(report, args.html)
                print(f"HTML REPORT {args.html!s}: "
                      f"{'complete' if report.complete else 'INCOMPLETE'}; "
                      "logical bytes only; no deletion or reclaimability claim")
            elif args.json:
                import json

                def as_item(item):
                    return {
                        "path": str(item.path),
                        "logical_bytes": item.logical_bytes,
                        "allocated_bytes": item.allocated_bytes,
                    }

                print(json.dumps({
                    "root": str(report.root),
                    "complete": report.complete,
                    "entries_visited": report.entries_visited,
                    "files": report.files,
                    "directories": report.directories,
                    "skipped_links": report.skipped_links,
                    "skipped_special": report.skipped_special,
                    "logical_bytes": report.logical_bytes,
                    "allocated_bytes": report.allocated_bytes,
                    "largest_files": [as_item(item) for item in report.largest_files],
                    "largest_children": [as_item(item) for item in report.largest_children],
                    "issues": list(report.issues),
                }, sort_keys=True))
            else:
                estimate = (
                    str(report.allocated_bytes) if report.allocated_bytes is not None
                    else "unknown"
                )
                print(f"USAGE {str(report.root)!r}: {report.logical_bytes} logical bytes, "
                      f"{estimate} estimated allocated bytes, {report.files} file(s), "
                      f"{report.directories} directory/ies, {report.entries_visited} entries")
                for item in report.largest_children:
                    print(f"GROUP {item.logical_bytes} logical bytes: {str(item.path)!r}")
                for item in report.largest_files:
                    print(f"FILE {item.logical_bytes} logical bytes: {str(item.path)!r}")
                for issue in report.issues:
                    print(f"INCOMPLETE {issue!r}")
                print(
                    "Read-only snapshot; physical allocation is approximate or unknown. "
                    "No deletion or reclaimable-space claim."
                )
            return 0 if report.complete else 1

        if args.command == "cleanup":
            # Evidence-only review. Deliberately no deletion, move, cache
            # publication, organizer configuration, or authorization path.
            from .cleanup import review_cleanup

            report = review_cleanup(
                args.path, max_entries=args.max_entries,
                max_depth=args.max_depth, top=args.top,
            )
            usage_report = report.usage
            if args.json:
                import json

                print(json.dumps({
                    "root": str(usage_report.root),
                    "complete": usage_report.complete,
                    "entries_visited": usage_report.entries_visited,
                    "logical_bytes": usage_report.logical_bytes,
                    "allocated_bytes": usage_report.allocated_bytes,
                    "observations": [{
                        "path": str(item.path),
                        "logical_bytes": item.logical_bytes,
                        "deletion_eligible": item.deletion_eligible,
                        "reclaimable_bytes": item.reclaimable_bytes,
                        "blocked_reason": report.blocked_reason,
                    } for item in report.observations],
                    "issues": list(usage_report.issues),
                    "deletion_eligible": False,
                    "reclaimable_bytes": None,
                }, sort_keys=True))
            else:
                print(f"CLEANUP REVIEW {str(usage_report.root)!r}: "
                      f"{usage_report.entries_visited} entries, "
                      f"{usage_report.logical_bytes} logical bytes; "
                      "reclaimable bytes unknown")
                for item in report.observations:
                    print(f"BLOCKED {item.logical_bytes} logical bytes: "
                          f"{str(item.path)!r}; {report.blocked_reason}")
                for issue in usage_report.issues:
                    print(f"INCOMPLETE {issue!r}")
                print("No deletion eligible; no files changed by OCD. "
                      "Independent restore and explicit consent are still required.")
            return 0 if usage_report.complete else 1

        config = load_config(path)
        if args.command == "roots":
            if args.roots_command == "add":
                updated = add_root(config, args.source, args.destination, mode=args.mode,
                                   tags=_tags(args.tag), settle_seconds=args.settle_seconds)
                save_config(updated, path)
                print(f"Registered {updated.roots[-1].source} -> {updated.roots[-1].destination} [{args.mode}]")
                return 0
            for root in config.roots:
                print(f"{root.source} -> {root.destination} [{root.mode}] tags={root.tags}")
            if not config.roots:
                print("No registered roots")
            return 0

        if args.command == "apply" and not args.yes:
            raise ConfigError("apply requires explicit --yes; no moves performed")
        if args.command == "apply":
            roots = _select_roots(config.roots, args.root)
            root = roots[0]
            if root.mode != "apply":
                raise ConfigError("root is in observe mode; register it in apply mode before applying")
            plan = _plan(root)  # Recheck current filesystem; do not reuse stale plans.
            from .apply import apply_moves

            journal_path = path.parent / "journal.jsonl"
            result = apply_moves(root, plan.moves, journal_path)
            for hold in (*plan.holds, *result.holds):
                print(f"HOLD {hold.path}: {hold.reason}")
            print(f"Applied {len(result.moved)} move(s); "
                  f"{len(plan.holds) + len(result.holds)} hold(s); "
                  f"{len(result.rolled_back)} rolled back")
            if result.recovery_required:
                print(f"Manual recovery required; consult {journal_path}", file=sys.stderr)
                return 1
            if result.holds or result.rolled_back:
                print("Apply incomplete: operational holds or rollback occurred", file=sys.stderr)
                return 1
            return 0

        # The monitor revalidates each root during each poll and reports a
        # per-root error without stopping healthy roots; don't preempt it here.
        roots = _select_roots(config.roots, args.root, verify=args.command != "watch")
        if args.command == "duplicates":
            from .duplicates import scan_duplicates

            report = scan_duplicates(roots, max_files=args.max_files, max_entries=args.max_entries)
            for group in report.groups:
                print(f"DUPLICATE size={group.size} sha256={group.sha256} "
                      f"({len(group.files)} distinct file(s))")
                for duplicate in group.files:
                    print(f"FILE {duplicate.path}")
                print(f"{group.logical_duplicate_bytes} logical duplicate byte(s); "
                      f"{group.potential_reclaim_bytes} potential reclaim byte(s) (estimate)")
            for skipped in report.skipped_unstable:
                print(f"SKIPPED unstable file: {skipped}")
            print(f"{len(report.groups)} duplicate candidate group(s); "
                  f"{report.files_scanned} file(s) scanned, {report.files_hashed} hashed, "
                  f"{report.skipped_hardlinks} hardlink alias(es) skipped; "
                  f"{report.logical_duplicate_bytes} logical duplicate byte(s), "
                  f"{report.potential_reclaim_bytes} potential reclaim byte(s) (estimate); "
                  "no changes made")
            return 0
        if args.command == "watch":
            from threading import Event, TIMEOUT_MAX
            from .monitor import watch_roots

            if math.isfinite(args.interval) and args.interval > TIMEOUT_MAX:
                raise ConfigError(f"--interval cannot exceed {TIMEOUT_MAX:g} seconds")
            stop_event = Event()
            try:
                watch_roots(roots, _display_watch_event, stop_event=stop_event,
                            interval_seconds=args.interval)
            except KeyboardInterrupt:
                stop_event.set()
                print("Watch stopped; no changes made")
                return 130
            return 0
        if args.command == "scan":
            for root in roots:
                files = _scan(root)
                print(f"Root: {root.source} ({len(files)} file(s); no changes made)")
                for entry in files:
                    print(f"FILE {entry.path} ({entry.size} bytes)")
            return 0
        if args.command == "plan":
            for root in roots:
                _display_plan(root, _plan(root))
            return 0
        parser.error("unknown command")
    except (ConfigError, OSError, RuntimeError, ValueError) as exc:
        print(f"ocd: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
