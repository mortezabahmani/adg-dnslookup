#!/bin/sh
# ─────────────────────────────────────────────────────────────
#  adg_sync.sh — Sync DNS Rewrites to AdGuard Home (shell)
#
#  Designed for OpenWrt / BusyBox ash.  POSIX-compatible — no
#  bash-isms.  Only supports "online" mode (pulls pre-resolved
#  JSON from GitHub).
#
#  Dependencies: curl, jq
#
#  Configuration: either export env vars or create a .env file
#  next to this script.
#
#  Usage:
#    ./adg_sync.sh                 # normal run
#    DRY_RUN=1 ./adg_sync.sh      # preview only
# ─────────────────────────────────────────────────────────────
set -e

# ── Paths ─────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${LOG_FILE:-/tmp/adg_sync.log}"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"

# ── Logging helper ────────────────────────────────────────────
log() {
    _level="$1"; shift
    _ts="$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date)"
    _msg="${_ts}  ${_level}  $*"
    echo "$_msg" >> "$LOG_FILE"
    echo "$_msg"
}

log_info()  { log "INFO " "$@"; }
log_warn()  { log "WARN " "$@"; }
log_error() { log "ERROR" "$@"; }

# ── Load .env ─────────────────────────────────────────────────
if [ -f "$ENV_FILE" ]; then
    log_info "Loading config from $ENV_FILE"
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

# ── Required variables ────────────────────────────────────────
AGH_URL="${AGH_URL:?'AGH_URL not set (e.g. http://192.168.1.1:3000)'}"
AGH_USER="${AGH_USER:?'AGH_USER not set'}"
AGH_PASS="${AGH_PASS:?'AGH_PASS not set'}"
REPO="${REPO:?'REPO not set (e.g. user/adg-dnslookup)'}"
BRANCH="${BRANCH:-main}"
LISTS="${LISTS:-google,social,dev}"
DRY_RUN="${DRY_RUN:-0}"

# ── Dependency check ──────────────────────────────────────────
check_deps() {
    for cmd in curl jq; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            log_error "'$cmd' is not installed."
            if [ "$cmd" = "jq" ]; then
                log_error "Install with:  opkg update && opkg install jq"
            fi
            exit 1
        fi
    done
}

# ── AdGuard API helpers ──────────────────────────────────────
agh_get() {
    curl -s -f \
        -u "${AGH_USER}:${AGH_PASS}" \
        "${AGH_URL}$1"
}

agh_post() {
    _endpoint="$1"; _body="$2"
    curl -s -f \
        -u "${AGH_USER}:${AGH_PASS}" \
        -H "Content-Type: application/json" \
        -d "$_body" \
        "${AGH_URL}${_endpoint}"
}

# ── Download resolved JSON for a single list ─────────────────
download_list() {
    _list_name="$1"
    _url="https://raw.githubusercontent.com/${REPO}/${BRANCH}/resolved/${_list_name}.json"
    log_info "Downloading ${_list_name}.json ..."
    _data="$(curl -s -f "$_url" 2>/dev/null)" || {
        log_error "Failed to download ${_list_name}"
        echo "[]"
        return
    }
    echo "$_data"
}

# ── Main sync logic ──────────────────────────────────────────
sync() {
    log_info "════════════════════════════════════════════════════"
    log_info "  adg_sync.sh — starting sync"
    log_info "  AGH_URL=${AGH_URL}  REPO=${REPO}  BRANCH=${BRANCH}"
    log_info "  LISTS=${LISTS}"
    log_info "════════════════════════════════════════════════════"

    # ── Fetch current rewrites ────────────────────────────────
    log_info "Fetching current AdGuard rewrites ..."
    CURRENT="$(agh_get /control/rewrite/list)" || {
        log_error "Cannot connect to AdGuard Home at ${AGH_URL}"
        exit 1
    }
    CURRENT_COUNT="$(echo "$CURRENT" | jq 'length')"
    log_info "Current rewrites: ${CURRENT_COUNT}"

    # ── Collect all desired rewrites ──────────────────────────
    DESIRED="[]"
    # Split LISTS on commas (POSIX-safe)
    _old_ifs="$IFS"
    IFS=","
    for list_name in $LISTS; do
        IFS="$_old_ifs"
        list_name="$(echo "$list_name" | tr -d ' ')"
        [ -z "$list_name" ] && continue

        LIST_DATA="$(download_list "$list_name")"

        # Transform resolved JSON → [{domain, answer}] entries
        # Each entry has .domain, .ipv4[], .ipv6[]
        _entries="$(echo "$LIST_DATA" | jq -c '
            [.[] | select(.domain) |
                ((.ipv4 // [])[] as $ip | {domain: .domain, answer: $ip}),
                ((.ipv6 // [])[] as $ip | {domain: .domain, answer: $ip})
            ]
        ' 2>/dev/null || echo '[]')"

        _count="$(echo "$_entries" | jq 'length')"
        log_info "  ${list_name}: ${_count} rewrite entries"

        # Merge into DESIRED
        DESIRED="$(echo "$DESIRED" "$_entries" | jq -s '.[0] + .[1] | unique')"
    done
    IFS="$_old_ifs"

    DESIRED_COUNT="$(echo "$DESIRED" | jq 'length')"
    log_info "Total desired rewrites: ${DESIRED_COUNT}"

    # ── Collect all managed domains ───────────────────────────
    MANAGED_DOMAINS="$(echo "$DESIRED" | jq -r '[.[].domain] | unique | .[]')"

    # ── Compute additions ─────────────────────────────────────
    # Entries in DESIRED but not in CURRENT
    TO_ADD="$(echo "$CURRENT" "$DESIRED" | jq -s '
        (.[0] | map({domain, answer}) | unique) as $cur |
        (.[1] | map({domain, answer}) | unique) as $des |
        [$des[] | select(. as $d | $cur | map(select(.domain == $d.domain and .answer == $d.answer)) | length == 0)]
    ')"
    ADD_COUNT="$(echo "$TO_ADD" | jq 'length')"

    # ── Compute deletions ─────────────────────────────────────
    # Entries in CURRENT whose domain is managed but entry is not in DESIRED
    TO_DEL="$(echo "$CURRENT" "$DESIRED" | jq -s --argjson md "$(echo "$MANAGED_DOMAINS" | jq -R -s 'split("\n") | map(select(length > 0))')" '
        (.[1] | map({domain, answer}) | unique) as $des |
        [.[0][] | select(.domain as $d | $md | index($d)) |
            select(. as $c | $des | map(select(.domain == $c.domain and .answer == $c.answer)) | length == 0)]
    ')"
    DEL_COUNT="$(echo "$TO_DEL" | jq 'length')"

    log_info "Planned changes: +${ADD_COUNT} / -${DEL_COUNT}"

    if [ "$ADD_COUNT" -eq 0 ] && [ "$DEL_COUNT" -eq 0 ]; then
        log_info "Everything is up to date — no changes needed"
        return 0
    fi

    # ── Apply deletions ───────────────────────────────────────
    if [ "$DEL_COUNT" -gt 0 ]; then
        log_info "Applying ${DEL_COUNT} deletions ..."
        echo "$TO_DEL" | jq -c '.[]' | while IFS= read -r entry; do
            _domain="$(echo "$entry" | jq -r '.domain')"
            _answer="$(echo "$entry" | jq -r '.answer')"
            if [ "$DRY_RUN" = "1" ]; then
                log_info "  [DRY-RUN] DELETE  ${_domain} → ${_answer}"
            else
                log_info "  DELETE  ${_domain} → ${_answer}"
                agh_post "/control/rewrite/delete" "$entry" >/dev/null || \
                    log_error "  Failed to delete ${_domain}"
            fi
        done
    fi

    # ── Apply additions ───────────────────────────────────────
    if [ "$ADD_COUNT" -gt 0 ]; then
        log_info "Applying ${ADD_COUNT} additions ..."
        echo "$TO_ADD" | jq -c '.[]' | while IFS= read -r entry; do
            _domain="$(echo "$entry" | jq -r '.domain')"
            _answer="$(echo "$entry" | jq -r '.answer')"
            if [ "$DRY_RUN" = "1" ]; then
                log_info "  [DRY-RUN] ADD     ${_domain} → ${_answer}"
            else
                log_info "  ADD     ${_domain} → ${_answer}"
                agh_post "/control/rewrite/add" "$entry" >/dev/null || \
                    log_error "  Failed to add ${_domain}"
            fi
        done
    fi

    log_info "Sync complete: +${ADD_COUNT} / -${DEL_COUNT}"
}

# ── Entry point ───────────────────────────────────────────────
check_deps
sync
