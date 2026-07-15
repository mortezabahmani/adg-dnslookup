#!/bin/sh
# ─────────────────────────────────────────────────────────────
#  install_cron.sh — Install / uninstall cron job for adg_sync
#
#  Detects OS (Linux, macOS, OpenWrt) and installs the sync
#  script into the appropriate crontab.
#
#  Usage:
#    ./install_cron.sh              # interactive install
#    ./install_cron.sh --uninstall  # remove the cron job
# ─────────────────────────────────────────────────────────────
set -e

# ── Constants ─────────────────────────────────────────────────
CRON_TAG="# adg_sync — managed by install_cron.sh"
DEFAULT_SCHEDULE="0 */12 * * *"   # every 12 hours

# ── Colours (if terminal) ────────────────────────────────────
if [ -t 1 ]; then
    GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'
    BLUE='\033[34m'; BOLD='\033[1m'; RESET='\033[0m'
else
    GREEN=''; RED=''; YELLOW=''; BLUE=''; BOLD=''; RESET=''
fi

info()  { printf "${BLUE}ℹ️  %s${RESET}\n" "$*"; }
ok()    { printf "${GREEN}✅ %s${RESET}\n" "$*"; }
warn()  { printf "${YELLOW}⚠️  %s${RESET}\n" "$*"; }
err()   { printf "${RED}✗  %s${RESET}\n" "$*" >&2; }

# ── OS detection ──────────────────────────────────────────────
detect_os() {
    if [ -f /etc/openwrt_release ]; then
        OS="openwrt"
    elif [ "$(uname)" = "Darwin" ]; then
        OS="macos"
    elif [ "$(uname)" = "Linux" ]; then
        OS="linux"
    else
        OS="unknown"
    fi
}

# ── Crontab helpers ──────────────────────────────────────────
get_crontab_file() {
    case "$OS" in
        openwrt)
            echo "/etc/crontabs/root"
            ;;
        *)
            echo ""  # use crontab command instead
            ;;
    esac
}

read_current_crontab() {
    case "$OS" in
        openwrt)
            _file="$(get_crontab_file)"
            if [ -f "$_file" ]; then
                cat "$_file"
            fi
            ;;
        *)
            crontab -l 2>/dev/null || true
            ;;
    esac
}

write_crontab() {
    _content="$1"
    case "$OS" in
        openwrt)
            _file="$(get_crontab_file)"
            echo "$_content" > "$_file"
            # Restart crond on OpenWrt
            /etc/init.d/cron restart 2>/dev/null || true
            ;;
        *)
            echo "$_content" | crontab -
            ;;
    esac
}

# ── Uninstall ─────────────────────────────────────────────────
do_uninstall() {
    detect_os
    info "Detected OS: ${OS}"

    _current="$(read_current_crontab)"
    if echo "$_current" | grep -qF "$CRON_TAG"; then
        _new="$(echo "$_current" | grep -vF "$CRON_TAG")"
        # Also remove the command line that follows the tag
        # The tag and command are on the same line
        write_crontab "$_new"
        ok "Cron job removed successfully"
    else
        warn "No adg_sync cron job found — nothing to remove"
    fi
    exit 0
}

# ── Prompt helper ─────────────────────────────────────────────
prompt() {
    _prompt="$1"; _default="$2"
    if [ -n "$_default" ]; then
        printf "${BOLD}%s${RESET} [%s]: " "$_prompt" "$_default"
    else
        printf "${BOLD}%s${RESET}: " "$_prompt"
    fi
    read -r _answer
    if [ -z "$_answer" ]; then
        _answer="$_default"
    fi
    echo "$_answer"
}

# ── Install ───────────────────────────────────────────────────
do_install() {
    detect_os
    info "Detected OS: ${BOLD}${OS}${RESET}"

    # Check for existing installation
    _current="$(read_current_crontab)"
    if echo "$_current" | grep -qF "$CRON_TAG"; then
        warn "An adg_sync cron job already exists:"
        echo "$_current" | grep -F "$CRON_TAG"
        printf "\n"
        _replace="$(prompt "Replace it? [y/N]" "n")"
        case "$_replace" in
            y|Y|yes|YES)
                _current="$(echo "$_current" | grep -vF "$CRON_TAG")"
                ;;
            *)
                info "Keeping existing cron job. Exiting."
                exit 0
                ;;
        esac
    fi

    # Detect available sync scripts
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    _default_script=""
    if [ -f "${SCRIPT_DIR}/adg_sync.py" ]; then
        _default_script="${SCRIPT_DIR}/adg_sync.py"
    elif [ -f "${SCRIPT_DIR}/adg_sync.sh" ]; then
        _default_script="${SCRIPT_DIR}/adg_sync.sh"
    fi

    printf "\n"
    SYNC_SCRIPT="$(prompt "Path to sync script" "$_default_script")"

    if [ ! -f "$SYNC_SCRIPT" ]; then
        err "Script not found: $SYNC_SCRIPT"
        exit 1
    fi

    # Detect config path
    _default_config="${SCRIPT_DIR}/config.ini"
    CONFIG_PATH=""
    if echo "$SYNC_SCRIPT" | grep -q '\.py$'; then
        CONFIG_PATH="$(prompt "Path to config.ini" "$_default_config")"
    elif echo "$SYNC_SCRIPT" | grep -q '\.sh$'; then
        _default_env="${SCRIPT_DIR}/.env"
        CONFIG_PATH="$(prompt "Path to .env file" "$_default_env")"
    fi

    # Schedule
    SCHEDULE="$(prompt "Cron schedule" "$DEFAULT_SCHEDULE")"

    # Build the cron command
    if echo "$SYNC_SCRIPT" | grep -q '\.py$'; then
        # Python script
        _python="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo "python3")"
        if [ -n "$CONFIG_PATH" ] && [ -f "$CONFIG_PATH" ]; then
            CRON_CMD="${_python} ${SYNC_SCRIPT} --config ${CONFIG_PATH} --non-interactive"
        else
            CRON_CMD="${_python} ${SYNC_SCRIPT} --non-interactive"
        fi
    else
        # Shell script
        if [ -n "$CONFIG_PATH" ] && [ -f "$CONFIG_PATH" ]; then
            CRON_CMD="ENV_FILE=${CONFIG_PATH} ${SYNC_SCRIPT}"
        else
            CRON_CMD="${SYNC_SCRIPT}"
        fi
    fi

    # Ensure script is executable
    chmod +x "$SYNC_SCRIPT" 2>/dev/null || true

    # Build the cron line
    CRON_LINE="${SCHEDULE} ${CRON_CMD} ${CRON_TAG}"

    # Show preview
    printf "\n"
    info "The following cron job will be installed:"
    printf "\n"
    printf "  ${BOLD}%s${RESET}\n" "$CRON_LINE"
    printf "\n"

    case "$OS" in
        openwrt) info "Target: /etc/crontabs/root" ;;
        *)       info "Target: user crontab ($(whoami))" ;;
    esac

    printf "\n"
    _confirm="$(prompt "Install? [Y/n]" "y")"
    case "$_confirm" in
        n|N|no|NO)
            info "Cancelled."
            exit 0
            ;;
    esac

    # Write
    if [ -n "$_current" ]; then
        _new_crontab="$(printf '%s\n%s' "$_current" "$CRON_LINE")"
    else
        _new_crontab="$CRON_LINE"
    fi

    write_crontab "$_new_crontab"

    ok "Cron job installed successfully!"
    printf "\n"
    info "Verify with:"
    case "$OS" in
        openwrt)  printf "  cat /etc/crontabs/root\n" ;;
        *)        printf "  crontab -l\n" ;;
    esac
    printf "\n"
    info "To remove:  $0 --uninstall"
}

# ── Entry point ───────────────────────────────────────────────
case "${1:-}" in
    --uninstall|-u|uninstall)
        do_uninstall
        ;;
    --help|-h)
        printf "Usage:\n"
        printf "  %s              Install cron job (interactive)\n" "$0"
        printf "  %s --uninstall  Remove the cron job\n" "$0"
        printf "  %s --help       Show this help\n" "$0"
        exit 0
        ;;
    "")
        do_install
        ;;
    *)
        err "Unknown option: $1"
        printf "Use --help for usage.\n"
        exit 1
        ;;
esac
