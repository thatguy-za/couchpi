#!/usr/bin/env python3
"""CouchPi CEC daemon (runs as root).

- Turns TV-remote button presses (libcec's cec-client) into virtual keyboard
  events via uinput, so the remote works in the launcher AND inside apps.
- The Home/"root menu" button kills the running app (back to the launcher).
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
        code = KEYS.get(name)
        if code is not None:
            pressed = m.group(1) == "pressed"
            ui.write(e.EV_KEY, code, 1 if pressed else 0)
            ui.syn()
            if pressed and name == "root menu":
                kill_app()
        return
    low = line.lower()
    if "power status" in low:
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
            proc = subprocess.Popen(
                ["cec-client", "-d", "8", "-t", "p", "-o", "CouchPi"],
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
