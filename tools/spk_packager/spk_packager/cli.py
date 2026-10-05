from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from . import __version__
from .build import build_spk
from .lint import has_errors, lint_manifest
from .model import load_manifest
from .profiles import get_profile
from .scaffold import scaffold
from .verify import verify_spk


def _print_issues(issues) -> None:
    for issue in issues:
        print(f"{issue.severity.upper():7} {issue.code}: {issue.message}")


def command_init(args: argparse.Namespace) -> int:
    manifest = scaffold(
        Path(args.path),
        package_id=args.package_id,
        display_name=args.display_name or args.package_id,
        maintainer=args.maintainer,
    )
    print(manifest)
    return 0


def command_lint(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    issues = lint_manifest(manifest)
    if args.json:
        print(json.dumps([issue.as_dict() for issue in issues], indent=2))
    else:
        _print_issues(issues)
        if not issues:
            print("PASS")
    return 1 if has_errors(issues) else 0


def command_build(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    output = Path(args.output) if args.output else (
        manifest.root / "dist" /
        f"{manifest.package.package_id}-{manifest.package.arch[0]}-{manifest.package.version}.spk"
    )
    result = build_spk(manifest, output)
    if args.json:
        print(json.dumps({
            "output": str(result.output),
            "sha256": result.sha256,
            "outer_members": list(result.outer_members),
            "payload_members": list(result.payload_members),
        }, indent=2))
    else:
        print(result.output)
        print(f"sha256={result.sha256}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    report = verify_spk(
        Path(args.spk),
        profile_id=args.profile,
        strict=not args.compat_only,
        allow_noarch_native_bundle=args.allow_noarch_native_bundle,
    )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print("PASS" if report.ok else "FAIL")
        for message in report.errors:
            print(f"ERROR   {message}")
        for message in report.warnings:
            print(f"WARNING {message}")
        print(json.dumps(report.details, indent=2))
    return 0 if report.ok else 1


def command_explain(args: argparse.Namespace) -> int:
    profile = get_profile(args.profile)
    print(f"profile: {profile.profile_id}")
    print(f"minimum_os: {profile.minimum_os}")
    print(f"require_privilege: {profile.require_privilege}")
    print(f"require_icons: {profile.require_icons}")
    print(f"require_payload_checksum: {profile.require_payload_checksum}")
    print(f"wizard_uifiles_available: {profile.wizard_uifiles_available}")
    print(f"signing_deprecated: {profile.signing_deprecated}")
    print("notes:")
    for note in profile.notes:
        print(f"  - {note}")
    print("sources:")
    for source in profile.sources:
        print(f"  - {source}")
    return 0


def command_selftest(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="spk-packager-selftest-") as tmp:
        root = Path(tmp) / "example"
        manifest_path = scaffold(root, "SPKSelfTest", "SPK Self Test", "spk-packager")
        manifest = load_manifest(manifest_path)
        first = build_spk(manifest, root / "dist" / "selftest.spk")
        report = verify_spk(first.output)
        if not report.ok:
            print(json.dumps(report.as_dict(), indent=2))
            return 1
        second = build_spk(manifest, root / "dist" / "selftest-2.spk")
        if first.sha256 != second.sha256:
            print("deterministic rebuild mismatch", file=sys.stderr)
            return 1
        print(f"PASS sha256={first.sha256}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spk-packager")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a buildable DSM 7.2.2+ package scaffold")
    init.add_argument("path")
    init.add_argument("--package-id", required=True)
    init.add_argument("--display-name")
    init.add_argument("--maintainer", default="thebrazenbeard")
    init.set_defaults(func=command_init)

    lint = sub.add_parser("lint", help="validate a manifest and source inputs")
    lint.add_argument("manifest")
    lint.add_argument("--json", action="store_true")
    lint.set_defaults(func=command_lint)

    build = sub.add_parser("build", help="build a deterministic SPK")
    build.add_argument("manifest")
    build.add_argument("--output")
    build.add_argument("--json", action="store_true")
    build.set_defaults(func=command_build)

    verify = sub.add_parser("verify", help="verify a built SPK without extracting it")
    verify.add_argument("spk")
    verify.add_argument("--profile", default="dsm-7.2.2+")
    verify.add_argument("--compat-only", action="store_true")
    verify.add_argument(
        "--allow-noarch-native-bundle",
        action="store_true",
        help="allow ELF payloads under INFO arch=noarch for an explicitly multi-architecture bundle",
    )
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=command_verify)

    explain = sub.add_parser("explain", help="show the encoded compatibility contract and sources")
    explain.add_argument("profile", nargs="?", default="dsm-7.2.2+")
    explain.set_defaults(func=command_explain)

    selftest = sub.add_parser("selftest", help="scaffold, build twice, and verify determinism")
    selftest.set_defaults(func=command_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
