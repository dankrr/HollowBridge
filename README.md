# HollowBridge

HollowBridge helps Steam's **Install on Device** menu work with games managed
by Hollow. RemotePlaySync syncs the library, and HollowBridge asks the receiving
device's Hollow backend to prepare new Hollow-managed games. Once preparation
is complete, choose **Install on Device** in Steam; Steam downloads the game.

On its first run, HollowBridge records the existing library and only prepares
new eligible entries afterward. It leaves Steam-owned games to Steam when its
account-side Store check identifies them. That check is incomplete, so some
family, free, or temporary licenses may be uncertain.

## Requirements

On both devices, install and configure SLSsteam with plugins enabled,
[RemotePlaySync](https://github.com/dankrr/slssteam-remote-play-sync), Python 3,
Node.js 22 or newer, and systemd user services. Enable Steam Remote Play.
Install Hollow Desktop on the Linux desktop and Hollow Decky on the Steam Deck.

Steam's local debugging interface must be enabled on both devices. With Steam
fully closed, create the marker below, then start Steam and check that
`http://127.0.0.1:8080/json/list` is reachable locally:

```sh
touch ~/.local/share/Steam/.cef-enable-remote-debugging
```

This interface gives local software powerful access to Steam; enable it only
on devices where you trust the local software. HollowBridge does not expose it
over the network.

## Install

Connect both devices to the same local network. Configure RemotePlaySync on
both devices first; HollowBridge uses its existing peer configuration to
authenticate, so no additional pairing code is needed. Clone this repository
on each device:

```sh
git clone https://github.com/dankrr/HollowBridge.git
cd HollowBridge
```

**Exit Steam completely** on each device, then run the matching command from
its repository directory:

```sh
# Linux desktop:
./install.sh install desktop

# Steam Deck:
./install.sh install deck
```

Start Steam normally on both devices. HollowBridge discovers the configured
RemotePlaySync peer on the LAN and authenticates each request. Allow TCP and UDP port 39482 through
local firewalls; networks with client isolation or blocked broadcasts can
prevent discovery.

Requests are authenticated but not encrypted: game IDs and status can be
visible to others on the network. For private communication across networks,
use a trusted encrypted VPN, then add `PEER=hostname-or-ipv4` to
`~/.config/hollowbridge/native.conf` on each device with the other device's VPN
IPv4 address or hostname. Restart Steam after changing the configuration. HollowBridge does not
need a particular VPN. Hollow API keys and Steam credentials are not sent to
the peer.

## Update or uninstall

Exit Steam completely on each device. From its repository directory:

```sh
git pull
./install.sh update
# Or, to remove HollowBridge:
./install.sh uninstall
```

Update both devices before starting Steam again. The existing queue is kept.
Updates save a backup under `~/.local/share/hollowbridge-backups`. Uninstall
leaves those backups, other plugins, Hollow game data, and Steam's debugging
setting in place.

HollowBridge reads RemotePlaySync's existing peer IDs and secret from its
owner-only configuration file (or the corresponding `SLS_RPS_` environment
variables). Pairing in Steam alone does not authenticate a separate LAN
connection; RemotePlaySync must have matching peer settings on both devices.

Steam's local debugging and Decky interfaces may change with updates. Earlier
remote installs sometimes showed a timeout despite completing the download;
later tests succeeded in both directions, but the original cause is unknown.

## License

HollowBridge is available under the [MIT License](LICENSE). It is an independent
project and is not affiliated with Valve, SLSsteam, or Hollow.
