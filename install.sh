#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
plugin="$HOME/.config/SLSsteam/plugins/hollowbridge.lua"
state="$HOME/.local/state/hollowbridge"
cache="$HOME/.cache/hollowbridge"
legacy_project="$HOME/.local/share/hollowbridge"
legacy_config="$HOME/.config/hollowbridge"
legacy_unit="$HOME/.config/systemd/user/hollowbridge-steam.service"

fail() { printf '%s\n' "$*" >&2; exit 1; }

is_hollowbridge_plugin() {
    [[ -f $1 ]] && (
        grep -Fq -- '-- HollowBridge SLSsteam plugin.' "$1" ||
        grep -Fq -- '-- HollowBridge agent launcher.' "$1"
    )
}

stop_legacy_agent() {
    if command -v systemctl >/dev/null; then
        systemctl --user stop hollowbridge-steam.service 2>/dev/null || true
        systemctl --user disable hollowbridge-steam.service 2>/dev/null || true
    fi
    pkill -u "$(id -u)" -f 'python3.*-m hollowbridge(\.service)? serve' 2>/dev/null || true
    rm -f -- "$legacy_unit" \
        "$state/agent.lock" \
        "$state/agent.sock" \
        "$state/native-agent.pid" \
        "$state/requests.sqlite3" \
        "$state/requests.sqlite3-shm" \
        "$state/requests.sqlite3-wal" \
        "$state/sync-preparation.sqlite3" \
        "$state/sync-preparation.sqlite3-shm" \
        "$state/sync-preparation.sqlite3-wal"
    if command -v systemctl >/dev/null; then
        systemctl --user daemon-reload 2>/dev/null || true
    fi
}

check_hollow_backend() {
    local role=${1:-auto}
    local desktop_backend="$HOME/.local/share/hollow-desktop/current/backend/hollow/inject.py"
    local decky_backend="$HOME/homebrew/plugins/hollow-decky/hollow/inject.py"
    case $role in
        desktop)
            [[ -f $desktop_backend ]] || fail 'Hollow Desktop backend not found.'
            ;;
        deck)
            [[ -f $decky_backend ]] || fail 'Hollow Decky backend not found.'
            ;;
        auto)
            [[ -f $desktop_backend || -f $decky_backend ]] || fail 'Neither Hollow Desktop nor Hollow Decky backend was found.'
            ;;
        *)
            fail 'Choose desktop or deck, or omit the device role for automatic detection.'
            ;;
    esac
}

[[ $# -ge 1 ]] || fail 'Usage: ./install.sh install [desktop|deck] | update | uninstall'
action=$1
[[ $action == install || $action == update || $action == uninstall ]] || fail 'Unknown action.'

if [[ $action == uninstall ]]; then
    [[ $# == 1 ]] || fail 'Usage: ./install.sh uninstall'
    [[ ! -L $plugin && ! -L $state && ! -L $cache && ! -L $legacy_project && ! -L $legacy_config ]] || fail 'A HollowBridge path is a symlink; inspect it before uninstalling.'
    if [[ -e $plugin ]] && ! is_hollowbridge_plugin "$plugin"; then
        fail 'The plugin is not a recognized HollowBridge file; inspect it first.'
    fi
    stop_legacy_agent
    rm -f -- "$plugin"
    rm -rf -- "$state" "$cache" "$legacy_project" "$legacy_config"
    printf '%s\n' 'HollowBridge removed. Backups and Hollow game data were left in place.'
    exit 0
fi

for cmd in python3 install pgrep; do
    command -v "$cmd" >/dev/null || fail "Missing dependency: $cmd"
done
[[ -d $HOME/.config/SLSsteam/plugins ]] || fail 'SLSsteam plugin directory not found; install SLSsteam first.'
[[ -f $source_dir/sls-plugin/hollowbridge.lua ]] || fail 'Missing sls-plugin/hollowbridge.lua in source directory.'
[[ ! -L $plugin && ! -L $state ]] || fail 'A destination path is a symlink; inspect it first.'

if [[ $action == install ]]; then
    [[ $# -le 2 ]] || fail 'Usage: ./install.sh install [desktop|deck]'
    role=${2:-auto}
    check_hollow_backend "$role"
    [[ ! -e $plugin ]] || fail 'HollowBridge plugin already exists. Use ./install.sh update instead.'
    umask 077
    stop_legacy_agent
    install -d -m 700 "$state"
    install -m 600 "$source_dir/sls-plugin/hollowbridge.lua" "$plugin"
    rm -rf -- "$legacy_project" "$legacy_config" "$cache"
    printf '%s\n' 'Installed ~/.config/SLSsteam/plugins/hollowbridge.lua.'
else
    [[ $# == 1 ]] || fail 'Usage: ./install.sh update'
    check_hollow_backend auto
    [[ -f $plugin ]] || fail 'HollowBridge plugin is not installed; use ./install.sh install first.'
    is_hollowbridge_plugin "$plugin" || fail 'The installed plugin is not a recognized HollowBridge file.'
    umask 077
    backup_root="$HOME/.local/share/hollowbridge-backups"
    install -d -m 700 "$backup_root"
    backup=$(mktemp -d "$backup_root/update.XXXXXXXX")
    cp -a -- "$plugin" "$backup/hollowbridge.lua"
    [[ ! -d $legacy_project ]] || cp -a -- "$legacy_project" "$backup/project"
    [[ ! -d $legacy_config ]] || cp -a -- "$legacy_config" "$backup/config"
    [[ ! -f $legacy_unit ]] || cp -a -- "$legacy_unit" "$backup/hollowbridge-steam.service"
    stop_legacy_agent
    install -d -m 700 "$state"
    install -m 600 "$source_dir/sls-plugin/hollowbridge.lua" "$plugin"
    rm -rf -- "$legacy_project" "$legacy_config" "$cache"
    printf 'Updated ~/.config/SLSsteam/plugins/hollowbridge.lua; backup saved to %s\n' "$backup"
fi
