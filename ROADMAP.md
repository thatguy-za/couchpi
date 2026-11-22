# CouchPi roadmap

Gaps found by comparing CouchPi with LibreELEC, OSMC, Batocera, RetroPie/EmulationStation and
Apple TV-style launchers. This comes from a couple of web searches plus general knowledge of
those projects, so treat the priorities as opinions, not user research.

## High value (things most couch users expect)

1. ~~Bluetooth pairing~~ and ~~audio output picker + volume~~: done, including PIN/passkey pairing.
3. **App catalogue**: browse and one-click install more apps from the couch (Kodi, Plex HTPC,
   Moonlight, Steam Link, YouTube/Netflix via a browser kiosk, Spotify), not only
   `couchpi-apps add`.
4. **Phone/web companion**: a small web page on the LAN (`http://couchpi.local`) to type
   passwords, Jellyfin/server addresses and rename things with a real keyboard, and add apps.
5. **Gamepad as navigation + wake**: the pad already navigates the launcher; add "press a
   button to wake" and controller mappings for apps that need them.
6. **Safe updates**: keep the previous CouchPi version for rollback, and verify downloads
   (signed tags or checksums) instead of trusting `main`.

## Nice to have

- Screensaver / ambient mode when idle (Apple TV's biggest visual feature) and a boot splash.
- CEC extras: volume passthrough to the TV/soundbar, "power on TV when the Pi wakes", TV input
  switch on launch.
- Display extras: refresh-rate/HDR/overscan options, matching frame rate to content.
- Timezone, locale and keyboard layout in Settings; SSH on/off; Tailscale for remote access.
- Settings backup/restore and a one-click factory reset.
- Per-app launch options and re-ordering/hiding tiles from the UI.
- Recent items / "continue watching" row (needs Jellyfin API integration).
- Ready-made SD card image (Raspberry Pi Imager custom repo entry) for people who don't want
  to run a command at all.
- Kiosk / child mode (Batocera has one): lock settings behind a PIN.

## Hardware/support notes

- Verify CEC on the Pi 3B+ (legacy adapter) and tune Chiaki-ng expectations per model.
- Consider a lightweight fallback Jellyfin client for the 3B+ (1 GB RAM).
