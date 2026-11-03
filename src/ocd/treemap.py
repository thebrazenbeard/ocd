"""Offline, read-only treemap rendering for bounded OCD usage observations.

A static SVG remains useful without scripts. Reports with directory observations
also carry one hash-authorized offline drilldown script. Report publication never
changes the scanned tree, and byte counts never imply recoverable storage.
"""

from __future__ import annotations

import base64
import hashlib
from html import escape
import json
import os
from pathlib import Path
import stat

from .scanner import _is_link
from .usage import UsageReport


_PALETTE = (
    "#2c7da0", "#5b5f97", "#e09f3e", "#469d88", "#b56576",
    "#4d908e", "#8566aa", "#bc6c25", "#3d85c6", "#8d99ae",
)

_MAX_INTERACTIVE_DIRECTORIES = 2048

# A fixed, independently authored script. Its SHA-256 is placed in the CSP;
# scripts are never built from filesystem content or interpolated paths.
_DRILLDOWN_SCRIPT = r"""'use strict';
(() => {
  const dataElement = document.getElementById('ocd-directory-data');
  const stage = document.getElementById('ocd-directory-drilldown');
  if (!dataElement || !stage) return;
  const model = JSON.parse(dataElement.textContent);
  const nodes = model.nodes;
  const children = new Map();
  for (const node of nodes) children.set(node.id, []);
  for (const node of nodes) {
    if (node.parent !== null) children.get(node.parent).push(node);
  }
  const buttons = {
    home: document.getElementById('ocd-drill-home'),
    up: document.getElementById('ocd-drill-up'),
    crumbs: document.getElementById('ocd-drill-crumbs'),
    list: document.getElementById('ocd-drill-list'),
    svg: document.getElementById('ocd-drill-svg'),
    status: document.getElementById('ocd-drill-status'),
  };
  const ns = 'http://www.w3.org/2000/svg';
  let current = 0;
  const history = [];
  const make = (name, className, value) => {
    const element = document.createElement(name);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = value;
    return element;
  };
  const fmt = (size) => BigInt(size).toLocaleString('en-US') + ' bytes';
  const label = (node) => node.id === 0 ? model.root : node.name;
  const ancestors = (id) => {
    const path = [];
    let node = nodes[id];
    while (node) { path.push(node); node = node.parent === null ? null : nodes[node.parent]; }
    return path.reverse();
  };
  const enter = (id, focusReturn = null) => {
    if (focusReturn) history.push({ id: current, focusReturn });
    current = id;
    paint();
    buttons.status.focus();
  };
  const parent = () => {
    if (current === 0) return;
    const saved = history.pop();
    current = nodes[current].parent;
    paint();
    if (saved && saved.id === current && saved.focusReturn) {
      // Both list buttons and SVG tiles represent a directory. Restore focus
      // to the *kind* of control used, never the first document-wide match.
      const scope = saved.focusReturn.kind === 'tile' ? buttons.svg : buttons.list;
      for (const target of scope.querySelectorAll('[data-drill-id]')) {
        if (target.dataset.drillId === String(saved.focusReturn.id)) {
          target.focus(); return;
        }
      }
    }
    buttons.up.focus();
  };
  const home = () => { current = 0; history.length = 0; paint(); buttons.home.focus(); };
  const addButton = (node, text, className = '') => {
    const button = make('button', className, text);
    button.type = 'button';
    button.dataset.drillId = String(node.id);
    button.addEventListener('click', () => enter(node.id, { id: node.id, kind: 'list' }));
    return button;
  };
  const paint = () => {
    const node = nodes[current];
    buttons.crumbs.replaceChildren();
    for (const crumb of ancestors(current)) {
      const button = make('button', '', label(crumb));
      button.type = 'button';
      if (crumb.id === current) button.setAttribute('aria-current', 'location');
      button.addEventListener('click', () => enter(crumb.id));
      buttons.crumbs.append(button);
    }
    buttons.up.disabled = current === 0;
    buttons.list.replaceChildren();
    buttons.svg.replaceChildren();
    const subdirs = (children.get(current) || []).slice()
      .sort((a,b) => BigInt(a.size) === BigInt(b.size)
        ? a.name.localeCompare(b.name) : BigInt(a.size) > BigInt(b.size) ? -1 : 1);
    const options = subdirs.map(child => ({ node: child, name: child.name, size: child.size }));
    if (BigInt(node.direct) > 0n) options.push({ node: null, name: '(files directly here)', size: node.direct });
    if (BigInt(node.omitted) > 0n) options.push({ node: null, name: '(more observed subdirectories; details omitted)', size: node.omitted });
    if (!options.length) buttons.list.append(make('li', '', 'No regular-file bytes observed in this directory.'));
    for (const option of options) {
      const item = make('li', '');
      if (option.node) item.append(addButton(option.node, option.name));
      else item.append(make('span', '', option.name));
      item.append(make('span', 'ocd-drill-size', fmt(option.size)));
      buttons.list.append(item);
    }
    const area = options.filter(option => BigInt(option.size) > 0n);
    const total = area.reduce((sum, option) => sum + BigInt(option.size), 0n);
    let offset = 0;
    area.forEach((option, index) => {
      // BigInt retains exact byte labels; area is only a visual approximation.
      const width = Number(BigInt(option.size) * 1000000000n / total) / 1000000;
      const rect = document.createElementNS(ns, 'rect');
      rect.setAttribute('x', String(offset));
      rect.setAttribute('y', '0');
      rect.setAttribute('width', String(Math.max(0, width - 1)));
      rect.setAttribute('height', '380');
      rect.setAttribute('fill', ['#2c7da0','#5b5f97','#e09f3e','#469d88','#b56576'][index % 5]);
      const title = document.createElementNS(ns, 'title');
      title.textContent = option.name + ': ' + fmt(option.size);
      rect.append(title);
      if (option.node) {
        rect.setAttribute('role', 'button');
        rect.setAttribute('tabindex', '0');
        rect.setAttribute('aria-label', 'Open ' + option.name + ', ' + fmt(option.size));
        rect.addEventListener('click', () => enter(option.node.id, { id: option.node.id, kind: 'tile' }));
        rect.addEventListener('keydown', event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault(); enter(option.node.id, { id: option.node.id, kind: 'tile' });
          }
        });
      }
      buttons.svg.append(rect);
      offset += width;
    });
    buttons.status.textContent = 'Directory ' + label(node) + ': ' + fmt(node.size) +
      ' observed logical bytes. ' + (model.complete ? 'Complete scan.' : 'INCOMPLETE scan; totals can omit files.') +
      (model.truncated ? ' Interactive directory detail capped at ' + model.limit + ' entries.' : '');
  };
  buttons.up.addEventListener('click', parent);
  buttons.home.addEventListener('click', home);
  stage.addEventListener('keydown', event => {
    if (event.key === 'Escape' || event.key === 'Backspace') {
      if (event.target.isContentEditable || /^(INPUT|TEXTAREA)$/.test(event.target.tagName)) return;
      if (current !== 0) { event.preventDefault(); parent(); }
    }
  });
  paint();
  stage.hidden = false;
  const staticChart = document.getElementById('ocd-static-treemap');
  if (staticChart) staticChart.hidden = true;
})();"""


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


def _interactive_model(report: UsageReport) -> dict[str, object] | None:
    """Build a bounded BFS tree; excluded children remain disclosed as a group.

    The source hierarchy counts *logical path observations*, not allocated or
    reclaimable bytes. Directory completeness reflects the whole scan.
    """
    if not report.directory_totals:
        return None
    by_path = {item.path: item for item in report.directory_totals}
    if report.root not in by_path:
        return None
    children: dict[Path, list[Path]] = {}
    for path in by_path:
        if path == report.root:
            continue
        try:
            relative = path.relative_to(report.root)
        except ValueError:
            continue
        if not relative.parts or path.parent not in by_path:
            continue
        children.setdefault(path.parent, []).append(path)

    selected = [report.root]
    index = {report.root: 0}
    position = 0
    while position < len(selected):
        path = selected[position]
        for child in sorted(children.get(path, ()), key=lambda item: (-by_path[item].logical_bytes, str(item))):
            if len(selected) >= _MAX_INTERACTIVE_DIRECTORIES:
                break
            index[child] = len(selected)
            selected.append(child)
        position += 1
    nodes: list[dict[str, object]] = []
    for path in selected:
        item = by_path[path]
        all_children = children.get(path, ())
        child_total = sum(by_path[child].logical_bytes for child in all_children)
        omitted_total = sum(by_path[child].logical_bytes for child in all_children if child not in index)
        nodes.append({
            "id": index[path],
            "parent": None if path == report.root else index[path.parent],
            "name": path.name,
            "size": str(item.logical_bytes),
            "direct": str(max(0, item.logical_bytes - child_total)),
            "omitted": str(omitted_total),
        })
    return {
        "root": str(report.root), "complete": report.complete,
        "limit": _MAX_INTERACTIVE_DIRECTORIES,
        "truncated": len(selected) < len(by_path), "nodes": nodes,
    }


def _json_for_html(value: dict[str, object]) -> str:
    """Inert JSON safe from HTML script-element terminators and raw surrogates."""
    serialized = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return (serialized.replace("<", r"\u003c").replace(">", r"\u003e")
            .replace("&", r"\u0026").replace("\u2028", r"\u2028")
            .replace("\u2029", r"\u2029"))


def _interactive_markup(model: dict[str, object]) -> tuple[str, str]:
    script_hash = base64.b64encode(hashlib.sha256(_DRILLDOWN_SCRIPT.encode("utf-8")).digest()).decode("ascii")
    csp = ("default-src 'none'; script-src 'sha256-" + script_hash + "'; "
           "connect-src 'none'; img-src 'none'; object-src 'none'; "
           "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
    model_text = _json_for_html(model)
    markup = (
        '<section id="ocd-directory-drilldown" hidden aria-label="Interactive directory drilldown">'
        '<h2>Directory drilldown (observed logical bytes)</h2>'
        '<p class="meta">Keyboard: Tab to navigate, Enter/Space to open, Escape/Backspace to go up.</p>'
        '<p id="ocd-drill-status" role="status" tabindex="-1" aria-live="polite"></p>'
        '<nav aria-label="Directory breadcrumb"><button id="ocd-drill-home" type="button">Home</button>'
        '<button id="ocd-drill-up" type="button">Up</button><div id="ocd-drill-crumbs"></div></nav>'
        '<svg id="ocd-drill-svg" viewBox="0 0 1000 380" role="group" '
        'aria-label="Clickable observed directory sizes"></svg>'
        '<h3>Directory list (including zero-byte directories)</h3><ul id="ocd-drill-list"></ul>'
        '</section>'
        '<script id="ocd-directory-data" type="application/json">' + model_text + '</script>'
        '<script>' + _DRILLDOWN_SCRIPT + '</script>'
    )
    return csp, markup


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
    """Produce a self-contained HTML snapshot without opening any file."""
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

    model = _interactive_model(report)
    if model is None:
        csp = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
        interactive_markup = ""
    else:
        csp, interactive_markup = _interactive_markup(model)

    # Even when directory data is included, its serialized JSON is inert and
    # the only executable script is pinned to an exact CSP SHA-256.
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta http-equiv=\"Content-Security-Policy\" "
        f'content="{csp}">'
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
        ".empty{padding:48px;text-align:center;background:#192c3e}"
        "#ocd-directory-drilldown{margin:18px 0;padding:16px;background:#192c3e;"
        "border-radius:8px}#ocd-directory-drilldown nav{display:flex;gap:8px;"
        "align-items:center;flex-wrap:wrap;margin-bottom:14px}"
        "#ocd-drill-crumbs{display:flex;gap:5px;flex-wrap:wrap}"
        "#ocd-directory-drilldown button{background:#234c68;color:white;"
        "border:2px solid #79c6df;border-radius:5px;padding:6px 10px;"
        "cursor:pointer}#ocd-directory-drilldown button:disabled{opacity:.4;"
        "cursor:default}#ocd-directory-drilldown button:focus-visible,"
        "#ocd-drill-svg [role=button]:focus-visible{outline:3px solid #ffd166;"
        "outline-offset:3px}#ocd-drill-svg [role=button]{cursor:pointer}"
        "#ocd-drill-list{list-style:none;padding:0}#ocd-drill-list li{padding:6px;"
        "border-bottom:1px solid #405166;display:flex;gap:10px;"
        "justify-content:space-between;flex-wrap:wrap}"
        ".ocd-drill-size{font-variant-numeric:tabular-nums}</style></head><body>"
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
        '<h2>Top-level groups</h2><div id="ocd-static-treemap">' + diagram + '</div>'
        + interactive_markup
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
