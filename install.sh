#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
project="$HOME/.local/share/hollowbridge"
config="$HOME/.config/hollowbridge"
plugin="$HOME/.config/SLSsteam/plugins/hollowbridge.lua"
state="$HOME/.local/state/hollowbridge"
cache="$HOME/.cache/hollowbridge"
fail() { printf '%s\n' "$*" >&2; exit 1; }

[[ $# -ge 1 ]] || fail 'Usage: ./install.sh install desktop|deck PEER_TAILSCALE_IP  |  update  |  uninstall'
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

for cmd in python3 node tailscale systemctl install pgrep; do
    command -v "$cmd" >/dev/null || fail "Missing dependency: $cmd"
done
node -e 'if (typeof WebSocket !== "function" || typeof fetch !== "function") process.exit(1)' || fail 'Node.js needs built-in fetch and WebSocket support (Node 22 or newer).'
[[ -d $HOME/.config/SLSsteam/plugins ]] || fail 'SLSsteam plugin directory not found; install SLSsteam first.'
[[ -f $source_dir/hollowbridge/__main__.py && -f $source_dir/sls-plugin/hollowbridge.lua ]] || fail 'Incomplete HollowBridge source directory.'
[[ ! -L $project && ! -L $config && ! -L $plugin ]] || fail 'A destination path is a symlink; inspect it first.'

if [[ $action == install ]]; then
    [[ $# == 3 ]] || fail 'Usage: ./install.sh install desktop|deck PEER_TAILSCALE_IP'
    role=$2 peer=$3
    [[ $role == desktop || $role == deck ]] || fail 'Choose desktop or deck.'
    [[ ! -e $project && ! -e $config && ! -e $plugin ]] || fail 'HollowBridge files already exist. Back them up and use update, or inspect them first.'
    if [[ $role == desktop ]]; then
        kind=desktop
        [[ -f $HOME/.local/share/hollow-desktop/current/backend/hollow/inject.py ]] || fail 'Hollow Desktop backend not found.'
    else
        kind=decky
        [[ -d $HOME/homebrew/plugins/hollow-decky ]] || fail 'Hollow Decky backend not found.'
    fi
    bind=$(tailscale ip -4 | head -n 1)
    python3 - "$bind" "$peer" <<'PY'
import ipaddress, sys
try:
    local, peer = (ipaddress.ip_address(x) for x in sys.argv[1:])
    network = ipaddress.ip_network('100.64.0.0/10')
    if local.version != 4 or peer.version != 4 or local not in network or peer not in network or local == peer:
        raise ValueError
except ValueError:
    raise SystemExit('Use distinct Tailscale IPv4 addresses for this device and its peer.')
PY
    install -d -m 700 "$config" "$HOME/.local/share" "$project"
    cp -R -- "$source_dir/hollowbridge" "$project/"
    chmod 700 "$project"
    umask 077
    printf 'BIND=%s\nPEER=%s\nDEVICE=%s\nHOLLOW=%s\nPORT=39482\n' "$bind" "$peer" "$role" "$kind" > "$config/native.conf"
    install -m 600 "$source_dir/sls-plugin/hollowbridge.lua" "$plugin"
    printf '%s\n' 'Installed. Start Steam normally; repeat the install on the other device with this device’s Tailscale IP.'
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
    cp -R -- "$source_dir/hollowbridge/." "$project/hollowbridge/"
    install -m 600 "$source_dir/sls-plugin/hollowbridge.lua" "$plugin"
    printf 'Updated. Configuration and state were kept; backup: %s\n' "$backup"
fi
