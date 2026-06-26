#!/usr/bin/env bash
# Install or uninstall MedusaHC Klipper Python modules and config bundle.
#
# Moonraker update_manager parses PKGLIST below for OS package deps (script is not run).
PKGLIST=""
#
# One-line install (clones repo, then runs installer):
#   curl -fsSL https://raw.githubusercontent.com/topi314/MedusaHC/main/install.sh | bash -s -- --with-moonraker
#   wget -qO- https://raw.githubusercontent.com/topi314/MedusaHC/main/install.sh | bash -s -- --with-moonraker
#
# Install:
#   ./install.sh                  # scripts + config
#   ./install.sh --scripts-only   # Python modules only
#   ./install.sh --config-only    # config bundle only
#   ./install.sh --force          # overwrite existing bundle files (never saved_vars.cfg)
#   ./install.sh --symlink        # symlink scripts instead of copy (dev)
#   ./install.sh --with-moonraker # symlink scripts + add [update_manager medusahc]
#   ./install.sh --with-eddy      # also install probe_eddy_ng.py (see README)
#
# Uninstall:
#   ./install.sh --uninstall                  # remove scripts + config (with backup)
#   ./install.sh --uninstall --scripts-only
#   ./install.sh --uninstall --config-only
#   ./install.sh --uninstall --with-eddy    # restore probe_eddy_ng.py from .bak if present
#   ./install.sh --uninstall --remove-include  # also remove medusahc include from printer.cfg
#   ./install.sh --uninstall --remove-moonraker  # also remove update_manager section
#   ./install.sh --uninstall -y               # skip confirmation prompt
#
# Environment overrides:
#   KLIPPER_DIR=~/klipper
#   CONFIG_DIR=~/printer_data/config
#   MOONRAKER_CONF=~/printer_data/config/moonraker.conf
#   EDDY_NG_DIR=~/eddy-ng
#   MEDUSAHC_REPO_DIR=~/MedusaHC
#   MEDUSAHC_REPO_URL=https://github.com/topi314/MedusaHC.git
#   MEDUSAHC_REPO_BRANCH=main

set -euo pipefail

MEDUSAHC_REPO_DIR="${MEDUSAHC_REPO_DIR:-${HOME}/MedusaHC}"
MEDUSAHC_REPO_URL="${MEDUSAHC_REPO_URL:-https://github.com/topi314/MedusaHC.git}"
MEDUSAHC_REPO_BRANCH="${MEDUSAHC_REPO_BRANCH:-main}"

resolve_repo_root() {
    local script="${BASH_SOURCE[0]:-}"
    if [[ -z "$script" || "$script" == "bash" || ! -f "$script" ]]; then
        return 1
    fi
    local root
    root="$(cd "$(dirname "$script")" && pwd)"
    if [[ -f "${root}/scripts/medusahc.py" && -f "${root}/install.sh" ]]; then
        REPO_ROOT="$root"
        return 0
    fi
    return 1
}

bootstrap_repo() {
    local dir="${MEDUSAHC_REPO_DIR}"
    local url="${MEDUSAHC_REPO_URL}"
    local branch="${MEDUSAHC_REPO_BRANCH}"

    command -v git >/dev/null 2>&1 || die "git is required for one-line install"

    if [[ -d "${dir}/.git" ]]; then
        printf '==> Updating MedusaHC clone at %s\n' "$dir"
        git -C "$dir" fetch --depth 1 origin "${branch}"
        git -C "$dir" checkout "${branch}"
        git -C "$dir" reset --hard "origin/${branch}"
    elif [[ -e "$dir" ]]; then
        die "MEDUSAHC_REPO_DIR exists but is not a git repo: ${dir}"
    else
        printf '==> Cloning MedusaHC into %s\n' "$dir"
        git clone --depth 1 --branch "${branch}" "${url}" "${dir}"
    fi

    [[ -x "${dir}/install.sh" ]] || chmod +x "${dir}/install.sh"
    exec bash "${dir}/install.sh" "$@"
}

if ! resolve_repo_root; then
    bootstrap_repo "$@"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${KLIPPER_DIR:-${HOME}/klipper}"
CONFIG_DIR="${CONFIG_DIR:-${HOME}/printer_data/config}"
MOONRAKER_CONF="${MOONRAKER_CONF:-${CONFIG_DIR}/moonraker.conf}"
EDDY_NG_DIR="${EDDY_NG_DIR:-${HOME}/eddy-ng}"

INSTALL_SCRIPTS=1
INSTALL_CONFIG=1
UNINSTALL=0
FORCE=0
SYMLINK=0
WITH_MOONRAKER=0
WITH_EDDY=0
RESTART=1
RESTART_MOONRAKER=1
REMOVE_INCLUDE=0
REMOVE_MOONRAKER=0
ASSUME_YES=0

MEDUSAHC_SCRIPTS=(medusahc.py medusahc_calibrate.py)
INCLUDE_LINE="[include medusahc/medusahc.cfg]"
UPDATE_MANAGER_SECTION="[update_manager medusahc]"
DEFAULT_MOONRAKER_ORIGIN="https://github.com/topi314/MedusaHC.git"
DEFAULT_MOONRAKER_BRANCH="main"

usage() {
    sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage 0 ;;
        --uninstall) UNINSTALL=1 ;;
        --scripts-only) INSTALL_CONFIG=0 ;;
        --config-only) INSTALL_SCRIPTS=0 ;;
        --force) FORCE=1 ;;
        --symlink) SYMLINK=1 ;;
        --with-moonraker) WITH_MOONRAKER=1 ;;
        --with-eddy) WITH_EDDY=1 ;;
        --no-restart) RESTART=0 ;;
        --no-restart-moonraker) RESTART_MOONRAKER=0 ;;
        --remove-include) REMOVE_INCLUDE=1 ;;
        --remove-moonraker) REMOVE_MOONRAKER=1 ;;
        -y|--yes) ASSUME_YES=1 ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done

if [[ ! -t 0 ]]; then
    ASSUME_YES=1
fi

if [[ "$WITH_MOONRAKER" -eq 1 ]]; then
    SYMLINK=1
fi

log() { printf '==> %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

timestamp() { date +%Y%m%d%H%M%S; }

backup_file() {
    local f="$1"
    if [[ -f "$f" || -L "$f" ]]; then
        cp -a "$f" "${f}.bak.$(timestamp)"
    fi
}

confirm_uninstall() {
    if [[ "$ASSUME_YES" -eq 1 ]]; then
        return 0
    fi
    local what="MedusaHC components"
    [[ "$INSTALL_SCRIPTS" -eq 1 && "$INSTALL_CONFIG" -eq 0 ]] && what="MedusaHC Python modules"
    [[ "$INSTALL_SCRIPTS" -eq 0 && "$INSTALL_CONFIG" -eq 1 ]] && what="MedusaHC config bundle"
    printf 'Uninstall %s? [y/N] ' "$what"
    local reply
    read -r reply
    case "$reply" in
        y|Y|yes|YES) return 0 ;;
        *) die "Uninstall cancelled" ;;
    esac
}

install_script() {
    local src="$1"
    local name
    name="$(basename "$src")"
    local dest="${KLIPPER_DIR}/klippy/extras/${name}"

    [[ -f "$src" ]] || die "Missing ${src}"
    [[ -d "${KLIPPER_DIR}/klippy/extras" ]] || die "Klipper extras dir not found: ${KLIPPER_DIR}/klippy/extras"

    if [[ "$SYMLINK" -eq 1 ]]; then
        log "Link ${name} -> ${dest}"
        ln -sfn "$src" "$dest"
        return
    fi

    log "Install ${name} -> ${dest}"
    backup_file "$dest"
    cp -a "$src" "$dest"
}

uninstall_script() {
    local name="$1"
    local dest="${KLIPPER_DIR}/klippy/extras/${name}"

    if [[ ! -e "$dest" ]]; then
        warn "Not installed (skip): ${dest}"
        return
    fi

    log "Remove ${dest}"
    backup_file "$dest"
    rm -f "$dest"
}

install_config_file() {
    local src="$1"
    local dest="$2"

    [[ -f "$src" ]] || die "Missing ${src}"
    if [[ -f "$dest" && "$FORCE" -eq 0 ]]; then
        log "Config exists (skip): ${dest}"
        return
    fi
    log "Install $(basename "$dest") -> ${dest}"
    if [[ -f "$dest" && "$FORCE" -eq 1 ]]; then
        backup_file "$dest"
    fi
    cp -a "$src" "$dest"
}

install_config_tree() {
    local src="${REPO_ROOT}/config/medusahc"
    local dest="${CONFIG_DIR}/medusahc"
    local f name target installed=0 skipped=0

    [[ -d "$src" ]] || die "Missing ${src}"
    mkdir -p "$dest"

    for f in "$src"/*; do
        [[ -f "$f" ]] || continue
        name="$(basename "$f")"
        target="${dest}/${name}"

        if [[ "$name" == "saved_vars.cfg" && -f "$target" ]]; then
            log "User offsets preserved (skip): ${target}"
            skipped=$((skipped + 1))
            continue
        fi

        if [[ -f "$target" && "$FORCE" -eq 0 ]]; then
            log "Config exists (skip): ${target}"
            skipped=$((skipped + 1))
            continue
        fi

        if [[ -f "$target" && "$FORCE" -eq 1 ]]; then
            backup_file "$target"
        fi

        log "Install ${name} -> ${target}"
        cp -a "$f" "$target"
        installed=$((installed + 1))
    done

    if [[ "$installed" -eq 0 && "$skipped" -gt 0 ]]; then
        log "No config files overwritten (${skipped} skipped; use --force to replace templates)"
    fi
}

uninstall_config_tree() {
    local dest="${CONFIG_DIR}/medusahc"

    if [[ ! -d "$dest" ]]; then
        warn "Config bundle not found (skip): ${dest}"
        return
    fi

    local backup="${dest}.uninstall.bak.$(timestamp)"
    log "Backup config bundle -> ${backup}"
    cp -a "$dest" "$backup"
    log "Remove ${dest}"
    rm -rf "$dest"
}

install_eddy() {
    local src="${REPO_ROOT}/scripts/probe_eddy_ng.py"
    if [[ ! -d "$EDDY_NG_DIR" ]]; then
        warn "--with-eddy: EDDY_NG_DIR not found (${EDDY_NG_DIR}), skipping"
        return
    fi
    local dest="${EDDY_NG_DIR}/probe_eddy_ng.py"
    log "Install probe_eddy_ng.py -> ${dest}"
    backup_file "$dest"
    cp -a "$src" "$dest"
    warn "See README for optional eddy-ng printer.cfg setup"
}

uninstall_eddy() {
    if [[ ! -d "$EDDY_NG_DIR" ]]; then
        warn "EDDY_NG_DIR not found (${EDDY_NG_DIR}), skipping"
        return
    fi
    local dest="${EDDY_NG_DIR}/probe_eddy_ng.py"
    local latest_bak
    latest_bak="$(ls -1t "${dest}.bak."* 2>/dev/null | head -n1 || true)"

    if [[ -n "$latest_bak" ]]; then
        log "Restore probe_eddy_ng.py from ${latest_bak}"
        cp -a "$latest_bak" "$dest"
        return
    fi

    if [[ -f "$dest" ]]; then
        if [[ "$FORCE" -eq 1 ]]; then
            warn "No .bak found; --force removing ${dest}"
            backup_file "$dest"
            rm -f "$dest"
        else
            warn "No probe_eddy_ng.py backup in ${EDDY_NG_DIR}; leaving file in place"
            warn "Re-run with --force to remove it anyway"
        fi
    else
        warn "probe_eddy_ng.py not present (skip)"
    fi
}

check_printer_include() {
    local printer_cfg="${CONFIG_DIR}/printer.cfg"

    if [[ ! -f "$printer_cfg" ]]; then
        warn "No printer.cfg at ${printer_cfg}"
        warn "Add to your printer.cfg: ${INCLUDE_LINE}"
        return
    fi

    if grep -qF "$INCLUDE_LINE" "$printer_cfg" 2>/dev/null; then
        log "printer.cfg already includes medusahc bundle"
        return
    fi

    warn "printer.cfg does not include MedusaHC yet. Add this line:"
    warn "  ${INCLUDE_LINE}"
}

warn_remove_printer_include() {
    local printer_cfg="${CONFIG_DIR}/printer.cfg"

    if [[ ! -f "$printer_cfg" ]]; then
        return
    fi

    if grep -qF "$INCLUDE_LINE" "$printer_cfg" 2>/dev/null; then
        warn "Remove this line from ${printer_cfg}:"
        warn "  ${INCLUDE_LINE}"
        warn "Or re-run with --remove-include to remove it automatically"
    fi
}

remove_printer_include() {
    local printer_cfg="${CONFIG_DIR}/printer.cfg"

    if [[ ! -f "$printer_cfg" ]]; then
        warn "No printer.cfg at ${printer_cfg} (skip --remove-include)"
        return
    fi

    if ! grep -qF "$INCLUDE_LINE" "$printer_cfg" 2>/dev/null; then
        log "printer.cfg has no medusahc include (skip)"
        return
    fi

    log "Remove medusahc include from ${printer_cfg}"
    backup_file "$printer_cfg"
    grep -vF "$INCLUDE_LINE" "$printer_cfg" > "${printer_cfg}.tmp"
    mv "${printer_cfg}.tmp" "$printer_cfg"
}

git_https_origin() {
    local url="${1:-}"
    url="${url%.git}"
    if [[ "$url" =~ ^git@github.com:(.+)$ ]]; then
        printf 'https://github.com/%s.git\n' "${BASH_REMATCH[1]}"
        return
    fi
    if [[ "$url" =~ ^https?:// ]]; then
        [[ "$url" =~ \.git$ ]] && printf '%s\n' "$url" || printf '%s.git\n' "$url"
        return
    fi
    printf '%s\n' "$DEFAULT_MOONRAKER_ORIGIN"
}

detect_moonraker_repo_origin() {
    if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        local origin
        origin="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
        if [[ -n "$origin" ]]; then
            git_https_origin "$origin"
            return
        fi
    fi
    printf '%s\n' "$DEFAULT_MOONRAKER_ORIGIN"
}

detect_moonraker_primary_branch() {
    if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        local branch
        branch="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
        if [[ -n "$branch" && "$branch" != "HEAD" ]]; then
            printf '%s\n' "$branch"
            return
        fi
    fi
    printf '%s\n' "$DEFAULT_MOONRAKER_BRANCH"
}

install_update_manager() {
    local dest="$MOONRAKER_CONF"
    local origin branch repo_path

    [[ -f "$dest" ]] || die "moonraker.conf not found: ${dest}"

    if grep -qF "$UPDATE_MANAGER_SECTION" "$dest" 2>/dev/null; then
        log "moonraker.conf already has ${UPDATE_MANAGER_SECTION}"
        return
    fi

    origin="$(detect_moonraker_repo_origin)"
    branch="$(detect_moonraker_primary_branch)"
    repo_path="$REPO_ROOT"

    log "Add ${UPDATE_MANAGER_SECTION} to ${dest}"
    backup_file "$dest"
    {
        printf '\n'
        printf '%s\n' "$UPDATE_MANAGER_SECTION"
        printf 'type: git_repo\n'
        printf 'path: %s\n' "$repo_path"
        printf 'origin: %s\n' "$origin"
        printf 'primary_branch: %s\n' "$branch"
        printf 'is_system_service: False\n'
        printf 'managed_services: klipper\n'
    } >> "$dest"
}

remove_update_manager() {
    local dest="$MOONRAKER_CONF"

    if [[ ! -f "$dest" ]]; then
        warn "No moonraker.conf at ${dest} (skip --remove-moonraker)"
        return
    fi

    if ! grep -qF "$UPDATE_MANAGER_SECTION" "$dest" 2>/dev/null; then
        log "moonraker.conf has no medusahc update_manager section (skip)"
        return
    fi

    log "Remove ${UPDATE_MANAGER_SECTION} from ${dest}"
    backup_file "$dest"
    awk -v section="$UPDATE_MANAGER_SECTION" '
        $0 == section { skip=1; next }
        skip && /^[[:space:]]*$/ { skip=0; next }
        skip && /^[^[:space:]#]/ { skip=0 }
        skip { next }
        { print }
    ' "$dest" > "${dest}.tmp"
    mv "${dest}.tmp" "$dest"
}

warn_remove_update_manager() {
    local dest="$MOONRAKER_CONF"

    if [[ ! -f "$dest" ]]; then
        return
    fi

    if grep -qF "$UPDATE_MANAGER_SECTION" "$dest" 2>/dev/null; then
        warn "Remove ${UPDATE_MANAGER_SECTION} from ${dest}"
        warn "Or re-run with --remove-moonraker to remove it automatically"
    fi
}

maybe_restart_moonraker() {
    if [[ "$RESTART_MOONRAKER" -eq 0 ]]; then
        log "Skipping Moonraker restart (--no-restart-moonraker)"
        return
    fi

    if command -v systemctl >/dev/null 2>&1 && systemctl list-units --type=service --all 2>/dev/null | grep -q 'moonraker.service'; then
        log "Restarting moonraker.service"
        sudo systemctl restart moonraker || warn "Could not restart moonraker.service"
        return
    fi

    warn "Restart Moonraker manually if needed (Firmware Restart is not enough)"
}

maybe_restart_klipper() {
    if [[ "$RESTART" -eq 0 ]]; then
        log "Skipping Klipper restart (--no-restart)"
        return
    fi

    if command -v systemctl >/dev/null 2>&1 && systemctl list-units --type=service --all 2>/dev/null | grep -q 'klipper.service'; then
        log "Restarting klipper.service"
        sudo systemctl restart klipper || warn "Could not restart klipper.service"
        return
    fi

    warn "Restart Klipper manually (Firmware Restart in Mainsail/Fluidd, or restart klipper service)"
}

do_install() {
    log "MedusaHC install"
    log "Repo:        ${REPO_ROOT}"
    log "Klipper:     ${KLIPPER_DIR}"
    log "Config dir:  ${CONFIG_DIR}"

    if [[ "$INSTALL_SCRIPTS" -eq 1 ]]; then
        for name in "${MEDUSAHC_SCRIPTS[@]}"; do
            install_script "${REPO_ROOT}/scripts/${name}"
        done
        [[ "$WITH_EDDY" -eq 1 ]] && install_eddy
    fi

    if [[ "$INSTALL_CONFIG" -eq 1 ]]; then
        install_config_tree
        check_printer_include
    fi

    if [[ "$WITH_MOONRAKER" -eq 1 ]]; then
        install_update_manager
        maybe_restart_moonraker
        log "Moonraker will git-pull this repo and restart Klipper on update"
        log "Config bundle is install-once; updates do not overwrite your printer_data config"
    fi

    maybe_restart_klipper
    log "Done."
}

do_uninstall() {
    log "MedusaHC uninstall"
    log "Klipper:     ${KLIPPER_DIR}"
    log "Config dir:  ${CONFIG_DIR}"

    confirm_uninstall

    if [[ "$INSTALL_SCRIPTS" -eq 1 ]]; then
        for name in "${MEDUSAHC_SCRIPTS[@]}"; do
            uninstall_script "$name"
        done
        [[ "$WITH_EDDY" -eq 1 ]] && uninstall_eddy
    fi

    if [[ "$INSTALL_CONFIG" -eq 1 ]]; then
        uninstall_config_tree
        if [[ "$REMOVE_INCLUDE" -eq 1 ]]; then
            remove_printer_include
        else
            warn_remove_printer_include
        fi
    fi

    if [[ "$REMOVE_MOONRAKER" -eq 1 ]]; then
        remove_update_manager
        maybe_restart_moonraker
    else
        warn_remove_update_manager
    fi

    maybe_restart_klipper
    log "Uninstall done."
}

if [[ "$UNINSTALL" -eq 1 ]]; then
    do_uninstall
else
    do_install
fi
