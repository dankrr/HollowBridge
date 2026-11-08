# HollowBridge

HollowBridge helps Steam's **Install on Device** menu work with games managed
by Hollow. RemotePlaySync syncs the library; HollowBridge notices new
Hollow-managed entries and asks the receiving device's existing Hollow backend
to prepare them. Steam handles the download when you choose Install on Device.
Preparation does not download the game.

It runs alongside SLSsteam, RemotePlaySync, and Hollow Desktop or Hollow Decky.
Steam-owned games are left to Steam when the account-side Store signal is
available. That signal is incomplete, so family, free, or temporary licenses
may be uncertain; SLSsteam's local ownership reports aren't treated as proof.
The first run records the existing library and only prepares new eligible
entries afterward.

## Install

Do this on both devices. You'll need SLSsteam and RemotePlaySync already
working, Hollow Desktop on the desktop and Hollow Decky on the Deck, Python 3,
Node.js 22 or newer, systemd user services, and Tailscale connected on both
devices. The devices must be allowed to reach each other over Tailscale.

HollowBridge also needs Steam's local debugging interface at
`127.0.0.1:8080`. The tested setup had this enabled before installation. If it
isn't enabled, exit Steam and create `~/.local/share/Steam/.cef-enable-remote-debugging`,
then start Steam again to check it. This interface gives local applications
powerful access to Steam, so only enable it on a device whose local software
you trust. HollowBridge doesn't expose it over the network. The installer does
not change Steam settings or install the other dependencies.

Copy or clone this repository to each device. Find each device's Tailscale
IPv4 address with `tailscale ip -4`. **Exit Steam completely**, then run from
the repository directory:

```sh
# On the desktop, using the Deck's Tailscale IP:
./install.sh install desktop DECK_TAILSCALE_IP

# On the Deck, using the desktop's Tailscale IP:
./install.sh install deck DESKTOP_TAILSCALE_IP
```

Start Steam normally on both devices. The separate Lua plugin starts one
agent with Steam and stops it when Steam exits. The existing user service
manager restarts the agent if it fails. The agents authenticate their peer
through Tailscale; Hollow API keys and Steam credentials aren't sent between
devices. The Store check uses Steam's existing local session to query Steam.

## Update or remove

Exit Steam completely before changing the plugin, since SLSsteam reloads its
shared Lua state when plugin files change. From an updated repository copy:

```sh
./install.sh update
# Or, to remove HollowBridge:
./install.sh uninstall
```

Updates keep the configuration and queue and save a backup under
`~/.local/share/hollowbridge-backups`. Uninstall removes HollowBridge's plugin,
package, configuration, state, and cache, but leaves backups, other plugins,
and Hollow's game data alone. It also leaves Steam's debugging setting alone.
If a Tailscale address changes, update `BIND` or `PEER` in
`~/.config/hollowbridge/native.conf` while Steam is stopped.

Steam's local debugging and Decky interfaces can change with updates. Earlier
tests sometimes showed a Steam remote-install timeout despite completing the
download; later tests succeeded in both directions, but the original cause is
unknown.
