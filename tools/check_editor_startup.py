"""Pre-build, no-dependency guard for repository editor startup surfaces.

This source check detects the known concealed folder-open loader class. A
clean result is not a workstation forensic finding or a universal code audit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


# Independently corroborated matching assets in safe repository parents.
# A future legitimate font requires an explicit reviewed allowlist update;
# a 4-byte font "magic" can also be a runnable JavaScript identifier.
APPROVED_FONT_SHA256 = {
    "fa-brands-400.ttf": "a0c145382acfe59767d1487d6505f349665b685c2885cd91b621d141a292b0f8",
    "fa-regular-400.ttf": "f32b94a29060dfec0c797a1dfe111357eb1d682f1aca4ac4b95cf0f4c3e2bfa9",
    "fa-solid-900.ttf": "231d68cefbc6846fb78cafca8467401a4f371e226418662e8900e3d52adc00ab",
    "fa-brands-400.woff": "e836c5d5dfc13e18170c5bddefb4fdac7c65ac0ad43a4b431acdc4a98865a260",
    "fa-regular-400.woff": "8e2d96198711b63752e5f2f5987b8ffc536df6156a065bdbe28cb7231cad793d",
    "fa-solid-900.woff": "352f8ead280419dd953d2bc7843386bb9430e8152c2dbd2c2d2494f6b9567ee7",
    "fa-brands-400.woff2": "5d9190292acdd48ba0fc35080f7e7448f3cdf0d79199a4d23f0f49b5341fdf29",
    "fa-regular-400.woff2": "6a8c8e9e1e7f692c21af1956de163f3d026778e6449fe93a09a671847ca1ae65",
    "fa-solid-900.woff2": "7f4d3fd0a705dbf8403298aad91d5de6972e6b5d536068eba8b24954a5a0a8c7",
}

def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _strip_jsonc(source: str) -> str:
    """Strip JSONC comments and trailing commas without touching quoted strings."""
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            result.append(char)
            index += 1
        elif char == "/" and next_char == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
        elif char == "/" and next_char == "*":
            end = source.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated JSONC block comment")
            index = end + 2
        else:
            result.append(char)
            index += 1
    if in_string:
        raise ValueError("unterminated JSON string")
    cleaned = "".join(result)
    result = []
    index = 0
    in_string = False
    escaped = False
    while index < len(cleaned):
        char = cleaned[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            result.append(char)
        elif char == ",":
            lookahead = index + 1
            while lookahead < len(cleaned) and cleaned[lookahead].isspace():
                lookahead += 1
            if lookahead < len(cleaned) and cleaned[lookahead] in "}]":
                index += 1
                continue
            result.append(char)
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _inspect_document(relative: str, value: object, findings: list[str]) -> None:
    def walk(obj: object, location: str) -> None:
        if isinstance(obj, dict):
            for key, item in obj.items():
                position = f"{location}.{key}"
                if key == "task.allowAutomaticTasks" and item != "off":
                    findings.append(f"{relative}: automatic tasks not disabled at {position}")
                if key.casefold() == "runon" and str(item).casefold() == "folderopen":
                    findings.append(f"{relative}: editor folderOpen task at {position}")
                walk(item, position)
        elif isinstance(obj, list):
            for index, item in enumerate(obj):
                walk(item, f"{location}[{index}]")
        elif isinstance(obj, str):
            normalized = obj.casefold().replace("\\", "/")
            while "//" in normalized:
                normalized = normalized.replace("//", "/")
            if "public/fonts/" in normalized and any(
                normalized.endswith(extension)
                for extension in (".ttf", ".woff2", ".woff", ".lkf")
            ):
                findings.append(f"{relative}: executable-style reference to font asset at {location}")

    if not isinstance(value, dict):
        findings.append(f"{relative}: editor configuration must be a JSON object")
        return
    if relative == ".vscode/tasks.json" and not isinstance(value.get("tasks"), list):
        findings.append(f"{relative}: manual tasks must be a list")
    if relative == ".vscode/settings.json" and value.get("task.allowAutomaticTasks") != "off":
        findings.append(f"{relative}: repository source must not request automatic tasks")
    if relative == ".vscode/settings.json" and "tasks" in value:
        findings.append(f"{relative}: unexpected embedded task definitions")
    if relative.casefold().endswith(".code-workspace") and "tasks" in value:
        candidate = value["tasks"]
        if not isinstance(candidate, dict) or not isinstance(candidate.get("tasks"), list):
            findings.append(f"{relative}: invalid workspace task definition")
    walk(value, "root")


def inspect_source(root: Path) -> list[str]:
    """Inspect known editor-autoexecution surfaces without executing source."""
    if not root.is_dir() or root.is_symlink():
        return ["repository root missing or symlinked"]
    findings: list[str] = []
    # Source checkout can be opened from any nested folder as a workspace.
    # Never follow a source symlink: it may hide a task in a host-only target.
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            findings.append(f"{relative.as_posix()}: source symlink requires review")
        if path.name.casefold() == ".vscode" and path != root / ".vscode":
            findings.append(f"{relative.as_posix()}: alternate editor workspace root forbidden")
        if path.name.casefold().endswith(".lkf"):
            findings.append(f"{relative.as_posix()}: disguised executable asset forbidden")
        parts = tuple(part.casefold() for part in relative.parts)
        if path.is_file() and not path.is_symlink() and "public" in parts and "fonts" in parts:
            if path.suffix.casefold() in (".ttf", ".woff", ".woff2"):
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    expected = APPROVED_FONT_SHA256.get(path.name.casefold())
                    if expected != digest:
                        findings.append(f"{relative.as_posix()}: invalid font file signature or unapproved content")
                except OSError:
                    findings.append(f"{relative.as_posix()}: unreadable font asset")

    candidates = [root / ".vscode/tasks.json", root / ".vscode/settings.json"]
    # Dist/node_modules files may still be force-tracked or opened directly;
    # ignore only Git's own metadata, never a source directory name.
    candidates.extend(path for path in root.rglob("*")
                      if path.name.casefold().endswith(".code-workspace")
                      and ".git" not in path.relative_to(root).parts)
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.is_file():
            findings.append(f"{relative}: missing or symlinked editor configuration")
            continue
        try:
            document = json.loads(_strip_jsonc(path.read_text(encoding="utf-8-sig")),
                                  object_pairs_hook=_unique_object,
                                  parse_constant=_reject_nonfinite)
        except (OSError, UnicodeError, ValueError) as exc:
            findings.append(f"{relative}: unreadable or ambiguous editor configuration: {type(exc).__name__}")
            continue
        _inspect_document(relative, document, findings)
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python tools/check_editor_startup.py REPOSITORY_ROOT", file=sys.stderr)
        return 2
    findings = inspect_source(Path(args[0]))
    print(json.dumps({"status": "BLOCK" if findings else "PASS", "findings": findings},
                     sort_keys=True, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
