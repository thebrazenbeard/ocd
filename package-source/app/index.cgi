#!/bin/sh
set -u

AUTH_CGI="/usr/syno/synoman/webman/modules/authenticate.cgi"
CURL="/usr/bin/curl"
MAX_BODY=1048576

respond() {
    code="$1"
    message="$2"
    printf 'Status: %s\r\n' "$code"
    printf 'Content-Type: text/plain; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf 'X-Content-Type-Options: nosniff\r\n'
    printf '\r\n%s\n' "$message"
    exit 0
}

[ -x "$AUTH_CGI" ] || respond "503 Service Unavailable" "DSM authentication service unavailable"

DSM_USER="$("$AUTH_CGI" 2>/dev/null | tr -d '\r\n' || true)"
[ -n "$DSM_USER" ] || respond "403 Forbidden" "DSM authentication required"

GROUPS="$(id -nG "$DSM_USER" 2>/dev/null || true)"
printf '%s\n' "$GROUPS" | tr ' ' '\n' | grep -qx 'administrators' ||
    respond "403 Forbidden" "DSM administrator access required"

METHOD="${REQUEST_METHOD:-GET}"
REQUEST_PATH="${PATH_INFO:-/}"

case "$METHOD" in
    GET|POST|PUT|DELETE) ;;
    *) respond "405 Method Not Allowed" "Unsupported method" ;;
esac

case "$REQUEST_PATH" in
    /|/healthz|/api/v1/status|/api/v1/roots|/api/v1/plans|/api/v1/findings|/api/v1/settings/tmdb|/api/v1/reconcile|/api/v1/roots/*) ;;
    *) respond "404 Not Found" "Unknown OCD endpoint" ;;
esac

case "$METHOD" in
    POST|PUT|DELETE)
        [ "${HTTP_X_OCD_DSM:-}" = "1" ] ||
            respond "403 Forbidden" "Missing OCD DSM mutation header"
        ;;
esac

BODY_LENGTH="${CONTENT_LENGTH:-0}"
case "$BODY_LENGTH" in
    ''|*[!0-9]*) respond "400 Bad Request" "Invalid Content-Length" ;;
esac
[ "$BODY_LENGTH" -le "$MAX_BODY" ] ||
    respond "413 Payload Too Large" "Request body exceeds 1 MiB"

if [ ! -x "$CURL" ]; then
    CURL="$(command -v curl 2>/dev/null || true)"
fi
[ -n "$CURL" ] && [ -x "$CURL" ] ||
    respond "503 Service Unavailable" "DSM curl client unavailable"

HEADERS="$(mktemp /tmp/ocd-cgi-headers.XXXXXX)" ||
    respond "503 Service Unavailable" "Unable to allocate proxy state"
BODY="$(mktemp /tmp/ocd-cgi-body.XXXXXX)" || {
    rm -f "$HEADERS"
    respond "503 Service Unavailable" "Unable to allocate proxy state"
}
trap 'rm -f "$HEADERS" "$BODY"' 0 HUP INT TERM

URL="http://127.0.0.1:9157$REQUEST_PATH"
if [ "$METHOD" = "POST" ] || [ "$METHOD" = "PUT" ]; then
    "$CURL" --silent --show-error --max-time 15 \
        --dump-header "$HEADERS" --output "$BODY" \
        --request "$METHOD" \
        --header "Content-Type: ${CONTENT_TYPE:-application/json}" \
        --data-binary @- "$URL"
else
    "$CURL" --silent --show-error --max-time 15 \
        --dump-header "$HEADERS" --output "$BODY" \
        --request "$METHOD" "$URL"
fi
CURL_STATUS=$?
[ "$CURL_STATUS" -eq 0 ] ||
    respond "502 Bad Gateway" "OCD daemon is unavailable"

STATUS="$(awk '/^HTTP\// { code=$2 } END { print code }' "$HEADERS")"
[ -n "$STATUS" ] || STATUS=502
CTYPE="$(grep -i '^Content-Type:' "$HEADERS" | tail -n 1 | cut -d: -f2- | sed 's/^[[:space:]]*//;s/\r$//')"
[ -n "$CTYPE" ] || CTYPE="application/octet-stream"

printf 'Status: %s\r\n' "$STATUS"
printf 'Content-Type: %s\r\n' "$CTYPE"
printf 'Cache-Control: no-store\r\n'
printf 'X-Content-Type-Options: nosniff\r\n'
printf '\r\n'
cat "$BODY"
