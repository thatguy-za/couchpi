#!/usr/bin/env python3
"""CouchPi CEC daemon (runs as root).

- Turns TV-remote button presses (libcec's cec-client) into virtual keyboard
  events via uinput, so the remote works in the launcher AND inside apps.
- The Home/"root menu" button kills the running app (back to the launcher).
- Pointer mode: the remote's arrows move a mouse pointer and OK clicks (for apps like
  Chiaki-ng that expect a mouse). Switched on per app (apps.d "pointer = true"), from the
  phone companion, or with the config key "cec_pointer_key" (default: the yellow button).
- Watches TV power: when the TV goes to standby, applies the configured sleep
  behaviour (stop app + low-power CPU, optionally shut down after a delay).
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from evdev import UInput, ecodes as e

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pointer import Pointer  # noqa: E402

CFG_PATH = os.environ.get("COUCHPI_CONFIG", "/etc/couchpi/config.json")
RUN_DIR = os.environ.get("COUCHPI_RUN", "/run/couchpi")

KEYS = {
    "select": e.KEY_ENTER, "up": e.KEY_UP, "down": e.KEY_DOWN,
    "left": e.KEY_LEFT, "right": e.KEY_RIGHT, "exit": e.KEY_ESC,
    "root menu": e.KEY_HOME, "setup menu": e.KEY_MENU, "contents menu": e.KEY_MENU,
    "favorite menu": e.KEY_MENU, "play": e.KEY_PLAYPAUSE, "pause": e.KEY_PLAYPAUSE,
    "stop": e.KEY_STOP, "rewind": e.KEY_REWIND, "fast forward": e.KEY_FASTFORWARD,
    "forward": e.KEY_NEXTSONG, "backward": e.KEY_PREVIOUSSONG,
    **{str(n): getattr(e, f"KEY_{n}") for n in range(10)},
}
KEY_RE = re.compile(r"key (pressed|released): (.+?) \(\w+\)")

proc = None
tv_state = "unknown"
ui = UInput({e.EV_KEY: sorted(set(KEYS.values()))}, name="couchpi-cec")
mouse = UInput({e.EV_REL: [e.REL_X, e.REL_Y], e.EV_KEY: [e.BTN_LEFT]}, name="couchpi-cec-pointer")
POINTER_FILE = os.path.join(RUN_DIR, "pointer_mode")


def _move(dx, dy):
    if dx:
        mouse.write(e.EV_REL, e.REL_X, dx)
    if dy:
        mouse.write(e.EV_REL, e.REL_Y, dy)
    mouse.syn()


pointer = Pointer(_move)


def pointer_active():
    try:
        with open(POINTER_FILE) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def set_pointer(on):
    try:
        with open(POINTER_FILE, "w") as f:
            f.write("1" if on else "0")
    except OSError:
        pass
    if not on:
        pointer.clear()


def cfg():
    try:
        with open(CFG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cec_send(line):
    try:
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
    except (OSError, ValueError, AttributeError):
        pass


def kill_app():
    try:
        with open(os.path.join(RUN_DIR, "app.pid")) as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)
    except (OSError, ValueError):
        pass


def set_governor(gov):
    for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor"):
        try:
            with open(p, "w") as f:
                f.write(gov)
        except OSError:
            pass


def get_governor():
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") as f:
            return f.read().strip()
    except OSError:
        return "ondemand"


def handle_line(line):
    global tv_state
    m = KEY_RE.search(line)
    if m and cfg().get("cec_enabled", True):
        name = m.group(2).strip().lower()
        pressed = m.group(1) == "pressed"
        if name == str(cfg().get("cec_pointer_key", "F4 (yellow)")).lower():
            if pressed:
                set_pointer(not pointer_active())
            return
        if pointer_active() and name in ("up", "down", "left", "right"):
            (pointer.press if pressed else pointer.release)(name)
            return
        if pointer_active() and name == "select":
            mouse.write(e.EV_KEY, e.BTN_LEFT, 1 if pressed else 0)
            mouse.syn()
            return
        code = KEYS.get(name)
        if code is not None:
            ui.write(e.EV_KEY, code, 1 if pressed else 0)
            ui.syn()
            if pressed and name == "root menu":
                kill_app()
        return
    low = line.lower()
    if "power status" in low and not ("changed" in low and "tv (0)" not in low):  # ignore our own device's status
        tail = (low.split("to ")[-1] if "changed" in low else low.split(":")[-1]).strip().strip("'")
        if tail.startswith("standby"):
            tv_state = "standby"
        elif tail.startswith("on"):
            tv_state = "on"


def poll_power():
    while True:
        time.sleep(10)
        if cfg().get("cec_enabled", True):
            cec_send("pow 0")


def sleeper():
    since, saved_gov = None, None
    while True:
        time.sleep(5)
        c = cfg()
        mode = c.get("sleep_mode", "idle")
        if tv_state == "standby" and mode != "off":
            if since is None:
                since, saved_gov = time.time(), get_governor()
                kill_app()
                set_governor("powersave")
            if mode == "shutdown" and time.time() - since > int(c.get("sleep_minutes", 30)) * 60:
                subprocess.run(["systemctl", "poweroff"])
        elif tv_state == "on" and since is not None:
            set_governor(saved_gov or "ondemand")
            since = None
            cec_send("as")  # take the TV's input back to the Pi


def main():
    global proc
    threading.Thread(target=poll_power, daemon=True).start()
    threading.Thread(target=sleeper, daemon=True).start()
    while True:
        if not cfg().get("cec_enabled", True):
            time.sleep(10)
            continue
        try:
            # log level 20 = notice + debug: key presses are logged at debug level
            proc = subprocess.Popen(
                ["cec-client", "-d", "20", "-t", "p", "-o", "CouchPi"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as ex:
            print("cec-client failed to start:", ex, file=sys.stderr)
            time.sleep(30)
            continue
        threading.Timer(3, lambda: cec_send("as")).start()
        for line in proc.stdout:
            handle_line(line)
        print("cec-client exited; retrying in 15s", file=sys.stderr)
        time.sleep(15)


if __name__ == "__main__":
    main()
