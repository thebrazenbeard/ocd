from __future__ import annotations

import re
import shlex

from .model import Manifest


def handles_case_action(text: str, action: str) -> bool:
    return re.search(
        rf"(?:^|[|\s]){re.escape(action)}(?:\||\))",
        text,
        flags=re.MULTILINE,
    ) is not None

NOOP_LIFECYCLE_NAMES = (
    "preinst",
    "postinst",
    "preuninst",
    "postuninst",
    "preupgrade",
    "postupgrade",
)


def noop_script() -> bytes:
    return b"#!/bin/sh\nexit 0\n"


def _runtime_arg(value: str) -> str:
    if "{pkgdest}" not in value and "{pkgvar}" not in value:
        return shlex.quote(value)

    parts = re.split(r"(\{pkgdest\}|\{pkgvar\})", value)
    rendered: list[str] = []
    for part in parts:
        if not part:
            continue
        if part == "{pkgdest}":
            rendered.append('\"${SYNOPKG_PKGDEST}\"')
        elif part == "{pkgvar}":
            rendered.append('\"${SYNOPKG_PKGVAR}\"')
        else:
            rendered.append(shlex.quote(part))
    return "".join(rendered) or "''"


def _runtime_path(root_var: str, relative: str) -> str:
    return f'"${{{root_var}}}/"' + shlex.quote(relative)


def render_start_stop_status(manifest: Manifest) -> bytes:
    service = manifest.service
    if not service.enabled:
        return b"""#!/bin/sh
case "${1:-}" in
    prestart|prestop|start|stop|killall|log) exit 0 ;;
    status) exit 0 ;;
    *) exit 1 ;;
esac
"""

    command = service.command
    assert command is not None
    args = " ".join(_runtime_arg(arg) for arg in service.args)
    arg_suffix = f" {args}" if args else ""
    state_mkdir = "\n".join(
        f"        mkdir -p {_runtime_path('SYNOPKG_PKGVAR', item)}"
        for item in service.state_dirs
    )
    if state_mkdir:
        state_mkdir += "\n"

    script = f"""#!/bin/sh
set -eu

PIDFILE={_runtime_path("SYNOPKG_PKGVAR", service.pid_file)}
STARTFILE="$PIDFILE.start"
LOGFILE={_runtime_path("SYNOPKG_PKGVAR", service.log_file)}
BIN={_runtime_path("SYNOPKG_PKGDEST", command)}

proc_start_time() {{
    pid="$1"
    [ -r "/proc/$pid/stat" ] || return 1
    line="$(cat "/proc/$pid/stat" 2>/dev/null)" || return 1
    rest="${{line##*) }}"
    set -- $rest
    [ "$#" -ge 20 ] || return 1
    printf '%s\\n' "${{20}}"
}}

pidfile_process_exists() {{
    [ -f "$PIDFILE" ] || return 1
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null
}}

is_running() {{
    [ -f "$STARTFILE" ] || return 1
    pidfile_process_exists || return 1
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    expected="$(cat "$STARTFILE" 2>/dev/null || true)"
    [ -n "$expected" ] || return 1
    actual="$(proc_start_time "$pid" 2>/dev/null || true)"
    [ -n "$actual" ] && [ "$actual" = "$expected" ]
}}

report_start_failure() {{
    if [ -n "${{SYNOPKG_TEMP_LOGFILE:-}}" ]; then
        {{
            echo "Package service failed to start."
            if [ -r "$LOGFILE" ]; then
                echo
                echo "Last service log lines:"
                tail -n 100 "$LOGFILE" 2>/dev/null || true
            fi
        }} >"$SYNOPKG_TEMP_LOGFILE"
    fi
}}

stop_service() {{
    if ! is_running; then
        rm -f "$PIDFILE" "$STARTFILE"
        return 0
    fi
    pid="$(cat "$PIDFILE")"
    kill -TERM "$pid" 2>/dev/null || true
    i=0
    while is_running && [ "$i" -lt {service.stop_timeout_seconds} ]; do
        sleep 1
        i=$((i + 1))
    done
    if is_running; then
        kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE" "$STARTFILE"
}}

case "${{1:-}}" in
    prestart)
        [ -n "${{SYNOPKG_PKGDEST:-}}" ] || exit 1
        [ -n "${{SYNOPKG_PKGVAR:-}}" ] || exit 1
        [ -x "$BIN" ] || {{
            if [ -n "${{SYNOPKG_TEMP_LOGFILE:-}}" ]; then
                echo "Package executable is missing or not executable: $BIN" >"$SYNOPKG_TEMP_LOGFILE"
            fi
            exit 1
        }}
        exit 0
        ;;
    prestop)
        exit 0
        ;;
    start)
        if is_running; then
            exit 0
        fi
        if pidfile_process_exists; then
            if [ -n "${{SYNOPKG_TEMP_LOGFILE:-}}" ]; then
                echo "Refusing to start: PID file names a live process but its start-time identity is missing or does not match." >"$SYNOPKG_TEMP_LOGFILE"
            fi
            exit 1
        fi
        rm -f "$PIDFILE" "$STARTFILE"
        umask 077
        mkdir -p "${{SYNOPKG_PKGVAR}}"
{state_mkdir}        nohup "$BIN"{arg_suffix} >>"$LOGFILE" 2>&1 &
        pid="$!"
        echo "$pid" >"$PIDFILE"
        start_time="$(proc_start_time "$pid" 2>/dev/null || true)"
        if [ -z "$start_time" ]; then
            kill -TERM "$pid" 2>/dev/null || true
            rm -f "$PIDFILE" "$STARTFILE"
            report_start_failure
            exit 1
        fi
        echo "$start_time" >"$STARTFILE"
        sleep {service.start_probe_seconds}
        if is_running; then
            exit 0
        fi
        rm -f "$PIDFILE" "$STARTFILE"
        report_start_failure
        exit 1
        ;;
    stop)
        stop_service
        exit 0
        ;;
    killall)
        stop_service
        exit 0
        ;;
    status)
        if is_running; then
            exit 0
        fi
        if [ -f "$PIDFILE" ]; then
            exit 1
        fi
        exit 3
        ;;
    log)
        if [ -r "$LOGFILE" ]; then
            if [ -n "${{SYNOPKG_TEMP_LOGFILE:-}}" ]; then
                tail -n 100 "$LOGFILE" >"$SYNOPKG_TEMP_LOGFILE" 2>/dev/null || true
                echo "$SYNOPKG_TEMP_LOGFILE"
            else
                echo "$LOGFILE"
            fi
        fi
        exit 0
        ;;
    *)
        exit 1
        ;;
esac
"""
    return script.encode("utf-8")
