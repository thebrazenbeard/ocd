"""Offline, read-only treemap rendering for bounded OCD usage observations.

The HTML has no JavaScript or network dependencies. Rendering never changes the
scanned tree, and explicit publication refuses existing files and unsafe paths.
No logical or allocated byte count implies recoverable storage.
"""

from __future__ import annotations

from html import escape
import os
from pathlib import Path
import stat

from .scanner import _is_link
from .usage import UsageReport


_PALETTE = (
    "#2c7da0", "#5b5f97", "#e09f3e", "#469d88", "#b56576",
    "#4d908e", "#8566aa", "#bc6c25", "#3d85c6", "#8d99ae",
)


def _size(value: int) -> str:
    return f"{value:,} bytes"


def _html(value: str) -> str:
    """Escape markup and represent POSIX surrogateescape bytes as literal escapes."""
    return escape(value.encode("utf-8", "backslashreplace").decode("utf-8"))


def _within(path: Path, root: Path) -> bool:
    """Lexical or resolved containment; invalid/cross-volume pairs are outside."""
    try:
        child = os.path.normcase(str(path))
        base = os.path.normcase(str(root))
        return os.path.commonpath((child, base)) == base
    except ValueError:
        return False


def _tiles(report: UsageReport) -> list[tuple[str, int, float, float, float, float]]:
    """Balanced binary rectangle partition of observed top-level logical bytes."""
    groups: list[tuple[str, int]] = []
    reported = 0
    for item in report.largest_children:
        reported += item.logical_bytes
        label = "(files directly in root)" if item.path == report.root else item.path.name
        if item.logical_bytes > 0:
            groups.append((label, item.logical_bytes))
    remainder = max(0, report.logical_bytes - reported)
    if remainder:
        groups.append(("(other observed groups)", remainder))
    if not groups:
        return []

    result: list[tuple[str, int, float, float, float, float]] = []

    def partition(
        selected: list[tuple[str, int]], x: float, y: float, w: float, h: float,
    ) -> None:
        if len(selected) == 1:
            label, size = selected[0]
            result.append((label, size, x, y, w, h))
            return
        total = sum(item[1] for item in selected)
        # Choose the split closest to half while leaving both sides nonempty.
        running = 0
        choice = 1
        best = float("inf")
        for index in range(1, len(selected)):
            running += selected[index - 1][1]
            error = abs(total / 2 - running)
            if error < best:
                best = error
                choice = index
        before = selected[:choice]
        fraction = sum(item[1] for item in before) / total
        if w >= h:
            split = w * fraction
            partition(before, x, y, split, h)
            partition(selected[choice:], x + split, y, w - split, h)
        else:
            split = h * fraction
            partition(before, x, y, w, split)
            partition(selected[choice:], x, y + split, w, h - split)

    partition(groups, 0.0, 0.0, 1000.0, 580.0)
    return result


def render_usage_html(report: UsageReport) -> str:
    """Produce a self-contained static HTML snapshot without opening any file."""
    state = "COMPLETE" if report.complete else "INCOMPLETE"
    state_class = "complete" if report.complete else "incomplete"
    allocated = (
        _size(report.allocated_bytes) + " (estimate)"
        if report.allocated_bytes is not None else "unknown"
    )

    rectangles: list[str] = []
    for index, (label, size, x, y, w, h) in enumerate(_tiles(report)):
        # Inset each tile so boundaries are visible and do not overlap.
        dx = min(2.0, w / 4)
        dy = min(2.0, h / 4)
        x, y, w, h = x + dx, y + dy, max(0, w - 2 * dx), max(0, h - 2 * dy)
        color = _PALETTE[index % len(_PALETTE)]
        tooltip = _html(f"{label}: {_size(size)}")
        rectangles.append(
            f'<g><rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" '
            f'height="{h:.2f}" rx="4" fill="{color}">'
            f'<title>{tooltip}</title></rect>'
        )
        if w >= 135 and h >= 55:
            # Rectangle text is SVG-safe and never interpreted as markup.
            short_label = label[:28] + ("…" if len(label) > 28 else "")
            rectangles.append(
                f'<text x="{x + 10:.2f}" y="{y + 27:.2f}" '
                'fill="white" font-size="18" font-weight="600">'
                f'{_html(short_label)}</text>'
            )
            rectangles.append(
                f'<text x="{x + 10:.2f}" y="{y + 49:.2f}" '
                'fill="white" font-size="14">'
                f'{_html(_size(size))}</text>'
            )
        rectangles.append("</g>")

    if not rectangles:
        diagram = '<p class="empty">No regular-file bytes observed.</p>'
    else:
        diagram = (
            '<svg viewBox="0 0 1000 580" role="img" '
            'aria-label="Top-level groups scaled by observed logical bytes">'
            + "".join(rectangles) + "</svg>"
        )

    rows = "".join(
        "<tr><td>" + _html(str(item.path)) + "</td><td class=\"number\">"
        + _html(_size(item.logical_bytes)) + "</td></tr>"
        for item in report.largest_files
    ) or '<tr><td colspan="2">No regular files observed.</td></tr>'
    issues = "".join("<li>" + _html(issue) + "</li>" for issue in report.issues)
    issues_html = (
        '<section class="issues"><h2>Incomplete scan reasons</h2><ul>'
        + issues + "</ul></section>"
        if report.issues else ""
    )

    # The CSP and lack of script/embed/link elements forbid active content and
    # remote assets even when a scanned filename resembles HTML or JavaScript.
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta http-equiv=\"Content-Security-Policy\" "
        "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'\">"
        "<title>OCD Usage — read-only snapshot</title>"
        "<style>body{font:16px system-ui,sans-serif;background:#101a26;color:#f1f5f9;"
        "margin:0 auto;max-width:1200px;padding:24px}h1{margin:0 0 10px}"
        ".meta{color:#cbd5e1}.status{display:inline-block;padding:5px 10px;"
        "border-radius:6px;font-weight:bold}.complete{background:#12694c}"
        ".incomplete{background:#9b321f}.warning{background:#26364a;padding:16px;"
        "border-left:4px solid #f2aa4c;margin:16px 0}svg{display:block;width:100%;"
        "height:auto;background:#192c3e;border-radius:8px}table{width:100%;"
        "border-collapse:collapse;table-layout:fixed}th,td{padding:10px;"
        "border-bottom:1px solid #405166;text-align:left;overflow-wrap:anywhere}"
        ".number{text-align:right;width:190px}.issues{color:#ffd4b9}"
        ".empty{padding:48px;text-align:center;background:#192c3e}</style></head><body>"
        "<h1>OCD — disk usage snapshot</h1>"
        f'<p class="meta">Selected root: <code>{_html(str(report.root))}</code></p>'
        f'<p class="status {state_class}">{state}</p>'
        '<p class="warning"><strong>Read-only observation.</strong> '
        "Rectangle area represents observed <em>logical bytes</em>, not "
        "disk allocation or space safe to reclaim. Nothing is eligible "
        "for deletion based on this report. Incomplete reports exclude "
        "unscanned entries and cannot be treated as whole-disk totals.</p>"
        f'<p class="meta">{report.entries_visited:,} entries visited; '
        f'{report.files:,} files; {report.directories:,} directories; '
        f'{report.skipped_links:,} links skipped; {report.skipped_special:,} special skipped. '
        f'Observed logical total: {_size(report.logical_bytes)}. '
        f'Physical allocation: {_html(allocated)}.</p>'
        "<h2>Top-level groups</h2>" + diagram
        + "<h2>Largest observed files</h2><table><thead><tr>"
        "<th>File</th><th class=\"number\">Logical bytes</th>"
        "</tr></thead><tbody>" + rows + "</tbody></table>"
        + issues_html
        + '<p class="meta">Point-in-time bounded scan. No network, cleanup, '
        "moves, or authorization.</p></body></html>\n"
    )


def write_usage_html(report: UsageReport, output: Path) -> None:
    """Create one explicit new report outside scanned data; never overwrite."""
    output = Path(output)
    if not output.is_absolute() or not output.name:
        raise ValueError("--html must be an absolute output filename")
    if os.name == "nt":
        from .win32 import _path_parts

        _path_parts(str(output))  # Refuse ADS/device/ambiguous Win32 names.
    parent = output.parent
    if not parent.is_dir():
        raise ValueError("--html parent must be an existing real directory")

    # Reject symlinks and Windows reparse points in every ancestor.
    parent_identity = parent.lstat()
    for ancestor in (parent, *parent.parents):
        try:
            identity = ancestor.lstat()
        except OSError as exc:
            raise ValueError("--html parent cannot be inspected") from exc
        if _is_link(identity) or not stat.S_ISDIR(identity.st_mode):
            raise ValueError("--html parent contains a link or reparse point")
    # Check literal and resolved paths: an alias must not put output inside
    # the scanned root. This is still a stable, trusted-parent requirement on
    # Windows; no hostile concurrent junction-swap guarantee is claimed.
    real_parent = parent.resolve(strict=True)
    real_root = report.root.resolve(strict=True)
    if _within(output, report.root) or _within(real_parent, real_root):
        raise ValueError("--html output must be outside the selected scan root")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    payload = render_usage_html(report).encode("utf-8")
    parent_fd: int | None = None
    fd = -1
    try:
        if os.name != "nt":
            # Pin the verified parent on Linux: no pathname-based opening after
            # this point, even if an ancestor is swapped concurrently.
            directory_flags = (
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
            )
            parent_fd = os.open(parent, directory_flags)
            opened = os.fstat(parent_fd)
            if (opened.st_dev, opened.st_ino) != (
                parent_identity.st_dev, parent_identity.st_ino
            ):
                raise ValueError("--html output parent changed during validation")
            fd = os.open(output.name, flags, 0o600, dir_fd=parent_fd)
        else:
            # Windows has no qualified anchored creation path here; restrict
            # reports to trusted, stable parents and recheck before writing.
            fd = os.open(output, flags, 0o600)
            observed = parent.lstat()
            if (_is_link(observed) or
                (observed.st_dev, observed.st_ino) !=
                (parent_identity.st_dev, parent_identity.st_ino)):
                raise ValueError("--html output parent changed during publication")
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        # Never unlink by path on failure: another process may have replaced
        # that path. A partial report can remain for explicit operator review.
        raise
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
