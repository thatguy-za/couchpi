# CouchPi

An Apple-TV-style home screen for Raspberry Pi OS **Lite (64-bit)**. It boots straight into an
app launcher that you drive with your TV remote (HDMI-CEC), a keyboard or a gamepad.
Works on any 64-bit Pi (3B+, 4, 5). Ships with **Jellyfin Desktop** and **Chiaki-ng**.

## Install (one line)

Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager, connect the Pi to your network,
open a terminal on it (or SSH in) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/thatguy-za/couchpi/main/get.sh | sudo bash
```

It installs everything (including Jellyfin Desktop and Chiaki-ng) and reboots into CouchPi.
Set `COUCHPI_NO_REBOOT=1` in front of `bash` to skip the automatic reboot.

Prefer to read it first? Clone the repo and run `sudo ./install.sh`.

## Using it

- **Remote:** arrows + OK to navigate, Back/Exit to go back, **Home** (root menu) quits the
  running app and returns to the launcher.
- **Settings**
  - *Display:* output resolution (applied after a restart).
  - *TV off:* do nothing, go idle (stop the app, CPU to power-save), or shut down after a delay.
  - *HDMI-CEC remote:* on/off.
  - *Wi-Fi:* scan, pick a network and type the password with the on-screen keyboard.
  - *Device name:* change the Pi's hostname.
  - *Updates:* shows what's available for the OS, Jellyfin Desktop, Chiaki-ng and CouchPi, with
    update-now buttons. Auto-update: system (off / security only / all), each app on or off,
    daily or weekly. A timer runs at about 04:00; OS updates never reboot on their own.

## TV off = sleep

When the TV goes to standby (seen over CEC) CouchPi stops the running app and drops the CPU
to `powersave` ("Idle"), or shuts the Pi down after a delay ("Shut down"). A Pi can't suspend
to RAM, and a halted Pi can't be woken over CEC, so "Idle" is the mode that wakes automatically
when the TV comes back on.

## Adding apps

An app is one TOML file in `/opt/couchpi/apps.d/`:

```toml
name = "My App"
description = "Shown under the tile"
color = "#e11d48"
order = 30                      # position in the row
command = "flatpak run org.example.App"
# icon = "myapp.png"            # optional PNG, path relative to apps.d
# glyph = "play"                # or a built-in icon: play | gamepad | gear (else shows a letter)

[install]                       # optional, used by `couchpi-apps`
apt = ["some-package"]
flatpak = "org.example.App"
```

Then `sudo couchpi-apps add myapp.toml`. See `couchpi-apps list`.

## Develop on a laptop

```bash
COUCHPI_DEV=1 COUCHPI_APPS=./apps.d COUCHPI_CONFIG=./dev-config.json python3 launcher/launcher.py
```

Needs `pygame` (or `pygame-ce`). Arrow keys / Enter / Esc to navigate; `q` quits. Privileged
actions (Wi-Fi, updates) are faked in dev mode.

## Security notes

- Updates, including CouchPi's own, are downloaded from this repository's `main` branch and run
  as root. Only run CouchPi from a repository you trust, and leave "Auto-update CouchPi" off if
  you'd rather update by hand.
- The `couchpi` user may only run CouchPi's helper scripts (Wi-Fi, hostname, updates, display
  mode) and `reboot` through sudo.

## Caveats

- Untested on real hardware so far. Check CEC with `cec-client -l` (should list an adapter).
  Pi 4/5 use the kernel CEC driver; Pi 3B+ CEC support is the thing most likely to need tweaking.
- Chiaki-ng needs a fast Pi to stream well (Pi 5 recommended; Pi 3B+ is unlikely to cope).
- Wi-Fi settings need NetworkManager (the default on Raspberry Pi OS Bookworm and later).

See [ROADMAP.md](ROADMAP.md) for planned features. Uninstall with `sudo ./uninstall.sh`.

## Credits and licence

MIT licensed (see [LICENSE](LICENSE)). The Jellyfin icon is from
[jellyfin-ux](https://github.com/jellyfin/jellyfin-ux) under CC BY-SA 4.0
(see `apps.d/icons/ATTRIBUTION.md`). CouchPi is not affiliated with Jellyfin, Chiaki-ng or Sony.
