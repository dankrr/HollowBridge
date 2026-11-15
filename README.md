# HollowBridge

Use Steam's built-in **Install on Device** feature to install Hollow games between a Linux desktop and Steam Deck.

HollowBridge is a single-file Lua plugin for [SLSsteam](https://github.com/AceSLS/SLSsteam). It works with [RemotePlaySync](https://github.com/dankrr/slssteam-remote-play-sync) to automatically prepare Hollow games on the other device, allowing Steam to handle the installation normally.

No additional service, pairing code, or separate interface required.

## Requirements

- **SLSsteam** on both devices, with `Plugins`, `PlayNotOwnedGames`, and `API` enabled.
- **RemotePlaySync** installed and configured on both devices.
- **Hollow Desktop** on Linux or **Hollow Decky** on Steam Deck.
- **Hubcap API key** configured in Hollow on both devices.
- Python 3.

## Installation

Download [hollowbridge.lua](https://github.com/dankrr/HollowBridge/blob/main/hollowbridge.lua) and place it in your SLSsteam plugins folder:

```bash
mkdir -p ~/.config/SLSsteam/plugins
cp hollowbridge.lua ~/.config/SLSsteam/plugins/
chmod 600 ~/.config/SLSsteam/plugins/hollowbridge.lua
```

Install the same file on **both devices**. Restart Steam if the plugin isn't loaded automatically.

## Usage

1. Add a game through Hollow on either device.
2. RemotePlaySync synchronizes the library entry.
3. Open Steam and select **Install on Device**.
4. HollowBridge prepares the game on the destination device, and Steam handles the download.

No extra interface or manual preparation needed.

## Notes

- Steam and RemotePlaySync must be running on both devices.
- Both devices need a working Hubcap API key to retrieve the required game data.
- Games already installed or owned through Steam are left alone.
- Game files and credentials aren't transferred between devices.
- Games must have the required manifests and depot keys available through Hollow.
- Existing synced games can also be prepared when a remote installation is requested.

## Updating / Removing

To update, replace `hollowbridge.lua` with the latest version.

To uninstall, remove `~/.config/SLSsteam/plugins/hollowbridge.lua`.

## License

[MIT](LICENSE)
