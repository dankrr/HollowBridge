# HollowBridge

HollowBridge is a single-file [SLSsteam](https://github.com/AceSLS/SLSsteam) Lua
plugin (`sls-plugin/hollowbridge.lua`) that lets Steam's normal **Install on
Device** menu work bidirectionally between a Linux desktop (running Hollow
Desktop) and a Steam Deck (running Hollow Decky).

When [RemotePlaySync](https://github.com/dankrr/slssteam-remote-play-sync)
synchronizes a newly added game to the receiving device:

1. `hollowbridge.lua` observes the authenticated snapshot directly from
   `RemotePlaySync.remoteLibraries` inside SLSsteam's shared Lua state.
2. Genuinely owned and family-shared Steam games are skipped in-process via
   `CUser:isSubscribed(appId)`.
3. Eligible Hollow-managed games are prepared locally through the installed
   Hollow Desktop (`~/.local/share/hollow-desktop/current/backend`) or Hollow
   Decky (`~/homebrew/plugins/hollow-decky`) backend (registering
   `AdditionalPackageApps`, depot keys, and depot manifests, and setting Proton
   compatibility via `/tmp/SLSsteam.API` for Windows-only games).
4. On Steam startup, cached Hollow depot manifests are seeded back into
   `steamapps/depotcache/`.
5. If **Install on Device** is clicked before background preparation completes
   (or for a game from the initial baseline) and Steam creates an empty
   0-depot manifest (`StateFlags=4`, `BytesToDownload=0`), HollowBridge
   completes local Hollow preparation and triggers installation through
   `/tmp/SLSsteam.API`.

No separate network daemon, port, pairing key, systemd service, Node.js runtime,
or Chrome DevTools debugging port (`.cef-enable-remote-debugging`) is used. The
exact same `hollowbridge.lua` file runs on both desktop and Steam Deck.

## Requirements

- [SLSsteam](https://github.com/AceSLS/SLSsteam) with `PlayNotOwnedGames: yes`
  and `Api: yes` in `~/.config/SLSsteam/config.yaml`
- [RemotePlaySync](https://github.com/dankrr/slssteam-remote-play-sync)
  installed at `~/.config/SLSsteam/plugins/remote_play_sync.lua`
- Hollow Desktop on the Linux desktop, or Hollow Decky on the Steam Deck
- Python 3

## Install

Exit Steam completely on the device, then run:

```sh
./install.sh install
```

(`desktop` or `deck` may also be passed explicitly, e.g. `./install.sh install desktop`.)

Repeat on the other device, then start Steam normally. On its first run,
HollowBridge records the existing synchronized library as a baseline so it does
not mass-prepare historical entries.

## Update or Uninstall

Exit Steam completely, then run:

```sh
./install.sh update
# Or to remove HollowBridge:
./install.sh uninstall
```

## Testing

Run the offline test suite with:

```sh
python3 -m unittest discover -s tests -v
```

## License

HollowBridge is available under the [MIT License](LICENSE).
