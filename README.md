# HollowBridge

HollowBridge is a single-file [SLSsteam](https://github.com/AceSLS/SLSsteam) Lua plugin (`hollowbridge.lua`) that makes Steam's built-in **Install on Device** menu work in both directions between a Linux desktop and a Steam Deck for games added through Hollow.

When a game is added on one device and synchronized to the other by [RemotePlaySync](https://github.com/dankrr/slssteam-remote-play-sync), HollowBridge prepares the game locally through the target device's Hollow backend so Steam can download and install it normally.

## Requirements

- [SLSsteam](https://github.com/AceSLS/SLSsteam) with `Plugins: yes`, `PlayNotOwnedGames: yes`, and `API: yes` enabled in `~/.config/SLSsteam/config.yaml`
- [RemotePlaySync](https://github.com/dankrr/slssteam-remote-play-sync) installed at `~/.config/SLSsteam/plugins/remote_play_sync.lua`
- Hollow installed on each device:
  - **Linux Desktop**: Hollow Desktop (`~/.local/share/hollow-desktop/current/backend`)
  - **Steam Deck**: Hollow Decky (`~/homebrew/plugins/hollow-decky`)
- Python 3 (used by the local Hollow backend on each device)

## Installation

The same `hollowbridge.lua` file is used on both Arch Linux desktop and Steam Deck.

SLSsteam loads Lua plugins from `~/.config/SLSsteam/plugins/`. To install HollowBridge on either device, copy `hollowbridge.lua` into that directory:

```sh
mkdir -p ~/.config/SLSsteam/plugins
cp hollowbridge.lua ~/.config/SLSsteam/plugins/hollowbridge.lua
chmod 600 ~/.config/SLSsteam/plugins/hollowbridge.lua
```

Repeat the same step on the other device. If Steam is already running with SLSsteam plugins enabled, SLSsteam will load the plugin automatically; otherwise it will load on the next Steam launch.

## Updating and Uninstalling

### Update

Replace `~/.config/SLSsteam/plugins/hollowbridge.lua` with the latest `hollowbridge.lua`:

```sh
cp hollowbridge.lua ~/.config/SLSsteam/plugins/hollowbridge.lua
chmod 600 ~/.config/SLSsteam/plugins/hollowbridge.lua
```

### Uninstall

Remove the plugin file and its local state directory:

```sh
rm -f ~/.config/SLSsteam/plugins/hollowbridge.lua
rm -rf ~/.local/state/hollowbridge
```

If you previously installed an older service-based version of HollowBridge, you can also remove its old service and directories:

```sh
systemctl --user disable --now hollowbridge-steam.service 2>/dev/null || true
rm -f ~/.config/systemd/user/hollowbridge-steam.service
rm -rf ~/.local/share/hollowbridge ~/.config/hollowbridge
```

## How It Works

1. **Library Synchronization**: When you add a game through Hollow on your desktop or Steam Deck, `RemotePlaySync` syncs the updated `AdditionalApps` list to the other device over its existing connection.
2. **In-Process Observation**: `hollowbridge.lua` reads `RemotePlaySync.remoteLibraries` directly inside SLSsteam's shared Lua state. On the first observation of a peer, existing synchronized apps are recorded as a baseline so your entire library is not re-processed at startup.
3. **Ownership and Install Checks**: When a new AppID appears from a peer, HollowBridge checks `CUser:isSubscribed(appId)` in-process and inspects local `appmanifest_<appid>.acf` files. Games that your Steam account genuinely owns or receives via Family Sharing, or that are already installed locally with content, are skipped.
4. **Automatic Local Preparation**: For newly synced Hollow games, HollowBridge runs a one-shot background worker against the local Hollow Desktop or Hollow Decky backend to download depot manifests into `steamapps/depotcache/`, add the app to `AdditionalPackageApps`, write depot decryption keys to `~/.config/SLSsteam/config.yaml`, and configure Proton (`setcompat|<appid>|proton_experimental` via `/tmp/SLSsteam.API`) for Windows-only titles.
5. **Startup Manifest Seeding**: When Steam starts, HollowBridge seeds cached Hollow depot manifests back into `steamapps/depotcache/` so previously prepared games remain ready to install.
6. **Remote Installation and Empty-Install Recovery**: When you choose **Install on Device** in Steam, the destination machine already has the app registered, depot keys configured, and manifests in `depotcache/`, allowing Steam to download and install the game normally. If **Install on Device** is clicked before preparation has run (such as for a game from the initial baseline) and Steam creates an empty 0-byte manifest (`StateFlags "4"`, `SizeOnDisk "0"`, no `InstalledDepots`), HollowBridge detects the empty manifest, prepares the game through Hollow once, and triggers `install|<appid>|<libraryIndex>` via `/tmp/SLSsteam.API`.

## Limitations and Dependencies

- Both machines must be running Steam with SLSsteam and `RemotePlaySync` active for library updates and Steam remote-install requests to reach the target device.
- HollowBridge does not transfer game files or credentials between machines; each device prepares games locally using its own Hollow Desktop or Hollow Decky installation and cached manifests/keys.
- If Hollow does not have manifests or required base depot decryption keys for an AppID, preparation is skipped and `~/.config/SLSsteam/config.yaml` is left unchanged.
- Games that were already present when HollowBridge first recorded its baseline are not prepared in advance until either they are newly added or you select **Install on Device** for them.

## License

[MIT License](LICENSE)
