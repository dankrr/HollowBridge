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
Node.js 22 or newer, systemd user services, and Tailscale. Enable Steam Remote
Play and ensure the devices can reach one another over Tailscale. Install Hollow
Desktop on the Linux desktop and Hollow Decky on the Steam Deck.

Steam's local debugging interface must be enabled on both devices. With Steam
fully closed, create the marker below, then start Steam and check that
`http://127.0.0.1:8080/json/list` is reachable locally:

```sh
touch ~/.local/share/Steam/.cef-enable-remote-debugging
```

This interface gives local software powerful access to Steam; enable it only
on devices where you trust the local software. HollowBridge does not expose it
over the network.

## Install and configure

On each device, find its Tailscale IPv4 address with `tailscale ip -4`. Clone
this repository on both devices. **Exit Steam completely** before running the
installer:

```sh
git clone https://github.com/dankrr/HollowBridge.git
cd HollowBridge

# On the desktop, use the Deck's Tailscale IP:
./install.sh install desktop DECK_TAILSCALE_IP

# On the Deck, use the desktop's Tailscale IP instead:
./install.sh install deck DESKTOP_TAILSCALE_IP
```

Run only the command for the device you are configuring. Start Steam normally
on both devices. The plugin starts the companion with Steam; the devices
authenticate through Tailscale. Hollow API keys and Steam credentials stay on
their respective devices.

If an address changes, exit Steam and update `BIND` (this device) or `PEER`
(the other device) in `~/.config/hollowbridge/native.conf`, then start Steam.

## Update or uninstall

Exit Steam completely on the device first. From its repository directory:

```sh
git pull
./install.sh update
# Or, to remove HollowBridge:
./install.sh uninstall
```

Updates retain configuration and state and save a backup under
`~/.local/share/hollowbridge-backups`. Uninstall leaves those backups, other
plugins, Hollow game data, and Steam's debugging setting in place.

Steam's local debugging and Decky interfaces may change with updates. Earlier
remote installs sometimes showed a timeout despite completing the download;
later tests succeeded in both directions, but the original cause is unknown.

## License

HollowBridge is available under the [MIT License](LICENSE). It is an independent
project and is not affiliated with Valve, SLSsteam, or Hollow.
