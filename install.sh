#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
project="$HOME/.local/share/hollowbridge"
config="$HOME/.config/hollowbridge"
plugin="$HOME/.config/SLSsteam/plugins/hollowbridge.lua"
state="$HOME/.local/state/hollowbridge"
cache="$HOME/.cache/hollowbridge"
fail() { printf '%s\n' "$*" >&2; exit 1; }

[[ $# -ge 1 ]] || fail 'Usage: ./install.sh install desktop|deck | update | uninstall'
action=$1
[[ $action == install || $action == update || $action == uninstall ]] || fail 'Unknown action.'
if pgrep -u "$(id -u)" -x steam >/dev/null; then
    fail 'Exit Steam completely before installing, updating, or uninstalling HollowBridge.'
fi
if systemctl --user is-active --quiet hollowbridge-steam.service; then
    fail 'The HollowBridge agent is still running. Wait for it to stop and try again.'
fi

if [[ $action == uninstall ]]; then
    [[ $# == 1 ]] || fail 'Usage: ./install.sh uninstall'
    [[ ! -L $plugin && ! -L $project && ! -L $config && ! -L $state && ! -L $cache ]] || fail 'A HollowBridge path is a symlink; inspect it before uninstalling.'
    if [[ -e $plugin ]] && ! grep -Fq -- '-- HollowBridge agent launcher.' "$plugin"; then
        fail 'The plugin is not a recognized HollowBridge launcher; inspect it first.'
    fi
    if [[ -e $project && ! -f $project/hollowbridge/__main__.py ]]; then
        fail 'The project directory is not recognized; inspect it first.'
    fi
    rm -f -- "$plugin"
    rm -rf -- "$project" "$config" "$state" "$cache"
    printf '%s\n' 'HollowBridge removed. Separate backups and Hollow game data were left in place.'
    exit 0
fi

for cmd in python3 node systemctl install pgrep; do
    command -v "$cmd" >/dev/null || fail "Missing dependency: $cmd"
done
node -e 'if (typeof WebSocket !== "function" || typeof fetch !== "function") process.exit(1)' || fail 'Node.js needs built-in fetch and WebSocket support (Node 22 or newer).'
[[ -d $HOME/.config/SLSsteam/plugins ]] || fail 'SLSsteam plugin directory not found; install SLSsteam first.'
[[ -f $source_dir/hollowbridge/__main__.py && -f $source_dir/sls-plugin/hollowbridge.lua ]] || fail 'Incomplete HollowBridge source directory.'
[[ ! -L $project && ! -L $config && ! -L $plugin ]] || fail 'A destination path is a symlink; inspect it first.'
PYTHONPATH="$source_dir" python3 -c 'from hollowbridge.transport import load_key; load_key()' || fail 'RemotePlaySync peer configuration is missing or inaccessible.'

if [[ $action == install ]]; then
    [[ $# == 2 ]] || fail 'Usage: ./install.sh install desktop|deck'
    role=$2
    [[ $role == desktop || $role == deck ]] || fail 'Choose desktop or deck.'
    [[ ! -e $project && ! -e $config && ! -e $plugin ]] || fail 'HollowBridge files already exist. Back them up and use update, or inspect them first.'
    if [[ $role == desktop ]]; then
        kind=desktop
        [[ -f $HOME/.local/share/hollow-desktop/current/backend/hollow/inject.py ]] || fail 'Hollow Desktop backend not found.'
    else
        kind=decky
        [[ -d $HOME/homebrew/plugins/hollow-decky ]] || fail 'Hollow Decky backend not found.'
    fi
    install -d -m 700 "$config" "$HOME/.local/share" "$project"
    cp -R -- "$source_dir/hollowbridge" "$project/"
    chmod 700 "$project"
    umask 077
    printf 'DEVICE=%s\nHOLLOW=%s\nPORT=39482\n' "$role" "$kind" > "$config/native.conf"
    install -m 600 "$source_dir/sls-plugin/hollowbridge.lua" "$plugin"
    printf '%s\n' 'Installed. Repeat on the other device, then start Steam.'
else
    [[ $# == 1 ]] || fail 'Usage: ./install.sh update'
    [[ -f $project/hollowbridge/__main__.py && -f $config/native.conf && -f $plugin ]] || fail 'HollowBridge installation is incomplete; inspect it first.'
    grep -Fq -- '-- HollowBridge agent launcher.' "$plugin" || fail 'The plugin is not a recognized HollowBridge launcher.'
    backup_root="$HOME/.local/share/hollowbridge-backups"
    install -d -m 700 "$backup_root"
    backup=$(mktemp -d "$backup_root/update.XXXXXXXX")
    cp -a -- "$project" "$backup/project"
    cp -a -- "$plugin" "$backup/hollowbridge.lua"
    cp -a -- "$config" "$backup/config"
    # Older configurations always included BIND alongside their Tailscale peer.
    if grep -q '^BIND=' "$config/native.conf"; then
        sed -i '/^BIND=/d; /^PEER=/d' "$config/native.conf"
    fi
    cp -R -- "$source_dir/hollowbridge/." "$project/hollowbridge/"
    install -m 600 "$source_dir/sls-plugin/hollowbridge.lua" "$plugin"
    printf 'Updated. Configuration and state were kept; backup: %s\n' "$backup"
fi
