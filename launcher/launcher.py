#!/usr/bin/env python3
"""CouchPi launcher: Apple-TV-style app row driven by arrow keys (CEC remote,
keyboard or gamepad). Picking an app writes its command to $COUCHPI_RUN/launch
and exits; the session supervisor then runs it and returns here afterwards."""
import glob
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib

import pygame

import config

DEV = bool(os.environ.get("COUCHPI_DEV"))
APPS_DIR = os.environ.get("COUCHPI_APPS", "/opt/couchpi/apps.d")
RUN_DIR = os.environ.get("COUCHPI_RUN", "/run/couchpi")
BIN_DIR = os.environ.get("COUCHPI_BIN", "/opt/couchpi/bin")
SYS_HELPER = os.path.join(BIN_DIR, "couchpi-sys")
UPDATER = os.path.join(BIN_DIR, "couchpi-update")
STATUS_FILE = os.path.join(RUN_DIR, "update.json")
LOG_FILE = os.path.join(RUN_DIR, "update.log")
BG_EVENT = pygame.USEREVENT + 1

KEYMAP = {
    pygame.K_LEFT: "left", pygame.K_RIGHT: "right", pygame.K_UP: "up",
    pygame.K_DOWN: "down", pygame.K_RETURN: "ok", pygame.K_KP_ENTER: "ok",
    pygame.K_SPACE: "ok", pygame.K_ESCAPE: "back", pygame.K_BACKSPACE: "back",
}

RES_OPTIONS = [
    ("auto", "Automatic"), ("1280x720@60", "720p 60Hz"),
    ("1920x1080@30", "1080p 30Hz"), ("1920x1080@60", "1080p 60Hz"),
    ("3840x2160@30", "4K 30Hz"), ("3840x2160@60", "4K 60Hz (Pi 4/5)"),
]
SLEEP_OPTIONS = [("off", "Never"), ("idle", "Idle (stop app, save power)"),
                 ("shutdown", "Shut down after delay")]
DELAY_OPTIONS = [(15, "15 min"), (30, "30 min"), (60, "1 hour"), (120, "2 hours")]
ONOFF = [(True, "On"), (False, "Off")]
AUTO_OS_OPTIONS = [("off", "Off"), ("security", "Security only"), ("all", "All updates")]
FREQ_OPTIONS = [("daily", "Daily"), ("weekly", "Weekly")]

KB_LAYERS = {
    "abc": [list("1234567890"), list("qwertyuiop"), list("asdfghjkl@"), list("zxcvbnm._-")],
    "sym": [list("1234567890"), list("!@#$%^&*()"), list("-_=+[]{}<>"), list("/\\|;:'\",?.")],
}
KB_BOTTOM = ["Shift", "abc/#+=", "Show", "Space", "Del", "Clear", "Cancel", "Done"]

BG_TOP, BG_BOTTOM = (22, 26, 48), (6, 8, 16)
TEXT, DIM = (240, 242, 250), (140, 148, 170)


class SafeDict(dict):
    def __missing__(self, key):
        return ""


def load_apps():
    apps = []
    for path in sorted(glob.glob(os.path.join(APPS_DIR, "*.toml"))):
        try:
            with open(path, "rb") as f:
                d = tomllib.load(f)
            if "name" not in d or "command" not in d:
                raise ValueError("needs 'name' and 'command'")
            d["_dir"] = os.path.dirname(path)
            apps.append(d)
        except Exception as e:  # a bad app file must not break the launcher
            print(f"skipping {path}: {e}", file=sys.stderr)
    apps.sort(key=lambda a: (a.get("order", 100), a["name"]))
    return apps


def mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def row(label, value=None, ok=None, change=None, hint=None):
    return {"label": label, "value": value, "ok": ok, "change": change, "hint": hint}


def bg(fn, cb):
    """Run fn in a thread; deliver cb(result) on the main loop."""
    def work():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            res = e
        pygame.event.post(pygame.event.Event(BG_EVENT, cb=cb, res=res))
    threading.Thread(target=work, daemon=True).start()


def root(script, *args, stdin=None, timeout=60):
    """Run a privileged helper through sudo; returns stdout."""
    if DEV:
        print("SUDO:", os.path.basename(script), args)
        time.sleep(0.5)
        if args[:1] == ("wifi-list",):
            return json.dumps([
                {"ssid": "Home-WiFi", "signal": 82, "secure": True, "active": True},
                {"ssid": "Neighbour 5G", "signal": 55, "secure": True, "active": False},
                {"ssid": "Cafe Guest", "signal": 40, "secure": False, "active": False}])
        return json.dumps({"ok": True, "msg": "ok (dev)"})
    return subprocess.run(["sudo", "-n", script, *args], input=stdin, capture_output=True,
                          text=True, timeout=timeout, check=True).stdout


def audio_state():
    """Output devices + default + volume, via pactl (PipeWire/PulseAudio)."""
    if DEV:
        return {"default": "hdmi", "vol": 65, "sinks": [
            {"name": "hdmi", "desc": "HDMI / DisplayPort"}, {"name": "analog", "desc": "Headphones (3.5mm)"},
            {"name": "bt", "desc": "Soundbar (Bluetooth)"}]}
    run = lambda *a: subprocess.run(["pactl", *a], capture_output=True, text=True, timeout=10, check=True).stdout
    sinks = json.loads(run("-f", "json", "list", "sinks"))
    default = run("get-default-sink").strip()
    cur = next((s for s in sinks if s["name"] == default), sinks[0] if sinks else None)
    vol = 0
    if cur and cur.get("volume"):
        vol = int(next(iter(cur["volume"].values()))["value_percent"].rstrip("%"))
    return {"default": cur["name"] if cur else "", "vol": vol,
            "sinks": [{"name": s["name"], "desc": s.get("description") or s["name"]} for s in sinks]}


def pactl_set(*args):
    if DEV:
        print("PACTL:", args)
        return
    subprocess.run(["pactl", *args], capture_output=True, timeout=10, check=True)


class Launcher:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("CouchPi")
        if DEV:
            self.screen = pygame.display.set_mode((1280, 720))
        elif os.environ.get("COUCHPI_WINDOWED"):
            # under a kiosk compositor a plain window is made fullscreen for us
            self.screen = pygame.display.set_mode((1920, 1080), pygame.RESIZABLE)
        else:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
        for i in range(pygame.joystick.get_count()):
            try:
                pygame.joystick.Joystick(i).init()
            except pygame.error:
                pass

        self.cfg = config.load()
        self.apps = load_apps()
        self.items = self.apps + [{"name": "Settings", "description": "Display, network, updates and more",
                                   "color": "#4b5563", "glyph": "gear", "_builtin": "settings"}]
        self.sel, self.anim = 0, 0.0
        self.power_focus = False
        self.view = "home"           # home | menu | keyboard
        self.menu, self.stack, self.kb = None, [], None
        self.reboot_pending = False
        self.net = {}
        self.wifi_nets = None
        self.audio = None            # None = loading, {} = unavailable
        self.bt_devs = None
        self.pair, self.pair_proc = None, None
        self.busy = False
        self._status, self._status_at = {}, 0.0
        self._log, self._log_at = "", 0.0
        self.message, self.message_until = "", 0

        self.layout()

    def layout(self):
        """(Re)compute everything that depends on the screen size."""
        self.W, self.H = self.screen.get_size()
        self.f_big = pygame.font.Font(None, int(self.H * 0.075))
        self.f_mid = pygame.font.Font(None, int(self.H * 0.048))
        self.f_small = pygame.font.Font(None, int(self.H * 0.034))
        self.bg = self._make_bg()
        self.icons = {}

    # ---------- helpers ----------
    def _make_bg(self):
        s = pygame.Surface((self.W, self.H))
        for y in range(self.H):
            pygame.draw.line(s, mix(BG_TOP, BG_BOTTOM, y / self.H), (0, y), (self.W, y))
        return s

    def notify(self, text, secs=4):
        self.message, self.message_until = text, time.time() + secs

    def text(self, font, s, color, **pos):
        surf = font.render(s, True, color)
        self.screen.blit(surf, surf.get_rect(**pos))

    def glyph(self, name, size):
        """Built-in line icons, drawn at 4x and scaled down for smooth edges."""
        s = size * 4
        surf = pygame.Surface((s, s), pygame.SRCALPHA)
        c, w = (255, 255, 255), max(2, s // 22)
        m = s // 2
        if name == "play":
            pygame.draw.circle(surf, c, (m, m), int(s * 0.46), w)
            pygame.draw.polygon(surf, c, [(s * 0.4, s * 0.3), (s * 0.4, s * 0.7), (s * 0.72, m)])
        elif name == "gamepad":
            body = pygame.Rect(0, 0, int(s * 0.9), int(s * 0.5))
            body.center = (m, m)
            pygame.draw.rect(surf, c, body, w, border_radius=int(s * 0.22))
            dx, dy, arm = s * 0.28, m, s * 0.07
            pygame.draw.rect(surf, c, (dx - arm, dy - arm * 2.6, arm * 2, arm * 5.2))
            pygame.draw.rect(surf, c, (dx - arm * 2.6, dy - arm, arm * 5.2, arm * 2))
            pygame.draw.circle(surf, c, (int(s * 0.72), int(m - s * 0.07)), int(s * 0.05))
            pygame.draw.circle(surf, c, (int(s * 0.64), int(m + s * 0.07)), int(s * 0.05))
        elif name == "gear":
            pts = []
            for i in range(16):
                a = math.pi * 2 * i / 16
                r = s * (0.46 if i % 2 == 0 else 0.34)
                for da in (-0.1, 0.1):
                    pts.append((m + r * math.cos(a + da), m + r * math.sin(a + da)))
            pygame.draw.polygon(surf, c, pts)
            pygame.draw.circle(surf, (0, 0, 0, 0), (m, m), int(s * 0.14))
        elif name == "power":
            w, r = max(3, s // 14), s * 0.34
            pts = [(m + r * math.cos(math.radians(-55 + 290 * k / 48)),
                    m + r * math.sin(math.radians(-55 + 290 * k / 48))) for k in range(49)]
            pygame.draw.lines(surf, c, False, pts, w)
            for pt in (pts[0], pts[-1], (m, m - s * 0.4)):
                pygame.draw.circle(surf, c, pt, w // 2)
            pygame.draw.line(surf, c, (m, m - s * 0.4), (m, m - s * 0.05), w)
        else:
            return None
        return pygame.transform.smoothscale(surf, (size, size))

    def icon_for(self, item, size):
        path = item.get("icon")
        if not path:
            g = item.get("glyph")
            if not g:
                return None
            key = ("glyph:" + g, size)
            if key not in self.icons:
                self.icons[key] = self.glyph(g, size)
            return self.icons[key]
        key = (path, size)
        if key not in self.icons:
            try:
                img = pygame.image.load(os.path.join(item.get("_dir", "."), path)).convert_alpha()
                w, h = img.get_size()
                k = min(size * 1.5 / w, size / h)  # keep aspect; wide logos may be 1.5x wider than tall
                self.icons[key] = pygame.transform.smoothscale(img, (max(1, int(w * k)), max(1, int(h * k))))
            except (pygame.error, OSError):
                self.icons[key] = None
        return self.icons[key]

    # ---------- state readers (cached) ----------
    def status(self):
        if time.time() - self._status_at > 1.0:
            self._status_at = time.time()
            try:
                with open(STATUS_FILE) as f:
                    self._status = json.load(f)
            except (OSError, ValueError):
                self._status = {}
        return self._status

    def progress_label(self):
        if not self.busy:
            return ""
        if time.time() - self._log_at > 1.0:
            self._log_at = time.time()
            try:
                with open(LOG_FILE) as f:
                    lines = f.read().strip().splitlines()
                self._log = lines[-1][:60] if lines else "Working..."
            except OSError:
                self._log = "Working..."
        return self._log

    def upd_label(self, key):
        s = self.status()
        if not s:
            return "Not checked yet"
        if key == "os":
            o = s.get("os", {})
            txt = "Up to date" if not o.get("total") else f"{o['total']} available" + (
                f" ({o['security']} security)" if o.get("security") else "")
            return txt + ("  ·  restart needed" if s.get("reboot_required") else "")
        return "Update available" if s.get(key) else "Up to date"

    def update_summary(self):
        s = self.status()
        if not s:
            return ""
        n = int(bool(s.get("os", {}).get("total"))) + sum(bool(s.get(k)) for k in ("jellyfin", "chiaki", "couchpi"))
        return f"{n} available" if n else "Up to date"

    def refresh_net(self):
        info = {}
        try:
            out = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                                 capture_output=True, text=True, timeout=5).stdout
            info["ssid"] = next((l.split(":", 1)[1] for l in out.splitlines() if l.startswith("yes:")), "")
            info["ip"] = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                                        timeout=5).stdout.split()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
        self.net = info

    def net_label(self):
        ssid, ip = self.net.get("ssid"), self.net.get("ip")
        if ssid:
            return f"{ssid}  ·  {ip}" if ip else ssid
        return f"Wired  ·  {ip}" if ip else "Not connected"

    # ---------- menus ----------
    def open_menu(self, title, rows, mid=None):
        if self.menu:
            self.stack.append(self.menu)
        self.menu = {"title": title, "rows": rows, "sel": 0, "id": mid}
        self.view = "menu"

    def back(self):
        if self.stack:
            self.menu = self.stack.pop()
            self.view = "menu"
        else:
            self.menu, self.view = None, "home"

    def save(self):
        try:
            config.save(self.cfg)
        except OSError:
            self.notify("Could not save settings (permissions)")

    def opt(self, label, key, options, after=None):
        vals = [v for v, _ in options]

        def change(step):
            cur = vals.index(self.cfg.get(key)) if self.cfg.get(key) in vals else 0
            self.cfg[key] = vals[(cur + step) % len(vals)]
            self.save()
            if after:
                after()

        def value():
            names = dict(options)
            return "‹  " + str(names.get(self.cfg.get(key), self.cfg.get(key))) + "  ›"
        return row(label, value=value, change=change)

    def open_settings(self):
        self.refresh_net()
        rows = [
            self.opt("Resolution", "resolution", RES_OPTIONS, after=self.apply_resolution),
            self.opt("When TV turns off", "sleep_mode", SLEEP_OPTIONS),
            self.opt("Shutdown delay", "sleep_minutes", DELAY_OPTIONS),
            self.opt("HDMI-CEC remote", "cec_enabled", ONOFF),
            row("Audio", value=self.audio_label, ok=self.open_audio, hint="Output device and volume"),
            row("Bluetooth", ok=self.open_bluetooth, hint="Pair controllers, remotes and speakers"),
            row("Wi-Fi", value=self.net_label, ok=self.open_wifi, hint="Press OK to choose a network"),
            row("Phone keyboard", value=lambda: "Type from your phone", ok=self.open_companion,
                hint="Use your phone as a keyboard, remote and touchpad"),
            row("Device name", value=socket.gethostname, ok=self.edit_hostname, hint="Press OK to rename this device"),
            row("Updates", value=self.update_summary, ok=self.open_updates),
            row("Restart now", value=lambda: "needed" if self.reboot_pending else "", ok=self.reboot),
            row("Back", ok=self.back),
        ]
        self.open_menu("Settings", rows, "settings")

    def apply_resolution(self):
        try:
            root(os.path.join(BIN_DIR, "apply-display"), self.cfg["resolution"])
            self.reboot_pending = True
            self.notify("Resolution will apply after a restart")
        except (subprocess.SubprocessError, OSError):
            self.notify("Could not change resolution")

    def reboot(self):
        self.power("reboot")

    def power(self, what):
        self.notify("Shutting down..." if what == "poweroff" else "Restarting...", 60)
        if DEV:
            print("POWER:", what)
            return
        subprocess.Popen(["sudo", "-n", "/usr/bin/systemctl", what])

    def open_power_menu(self):
        rows = [row("Shut down", ok=lambda: self.power("poweroff")),
                row("Restart", ok=lambda: self.power("reboot")),
                row("Cancel", ok=self.back)]
        self.power_focus = False
        self.open_menu("Power", rows, "power")
        self.menu["sel"] = 2  # start on Cancel so a stray OK press does nothing

    # ---------- audio ----------
    def audio_label(self):
        a = self.audio
        if not a:
            return ""
        return next((s["desc"] for s in a["sinks"] if s["name"] == a["default"]), "")[:32]

    def open_audio(self):
        self.audio = None
        self.open_menu("Audio", self.audio_rows(), "audio")
        self.load_audio()

    def load_audio(self):
        bg(audio_state, self.on_audio)

    def on_audio(self, res):
        self.audio = res if isinstance(res, dict) else {}
        if isinstance(res, Exception):
            self.notify("Audio isn't available (is PipeWire running?)")
        if self.menu and self.menu.get("id") == "audio":
            self.menu["rows"] = self.audio_rows()
            self.menu["sel"] = min(self.menu["sel"], len(self.menu["rows"]) - 1)

    def audio_rows(self):
        rows = []
        if self.audio is None:
            rows.append(row("Loading..."))
        elif not self.audio.get("sinks"):
            rows.append(row("No audio outputs found"))
        else:
            rows.append(row("Volume", value=lambda: f"‹  {self.audio['vol']}%  ›", change=self.change_volume))
            for s in self.audio["sinks"]:
                rows.append(row(s["desc"], value=lambda s=s: "Selected" if s["name"] == self.audio["default"] else "",
                                ok=lambda s=s: self.pick_sink(s["name"]), hint="Press OK to use this output"))
        rows.append(row("Back", ok=self.back))
        return rows

    def change_volume(self, step):
        self.audio["vol"] = max(0, min(100, self.audio["vol"] + 5 * step))
        vol = self.audio["vol"]
        bg(lambda: pactl_set("set-sink-volume", "@DEFAULT_SINK@", f"{vol}%"), lambda r: None)

    def pick_sink(self, name):
        self.audio["default"] = name
        bg(lambda: pactl_set("set-default-sink", name), lambda r: self.load_audio())

    # ---------- Bluetooth ----------
    def open_bluetooth(self):
        self.open_menu("Bluetooth", self.bt_rows(), "bt")
        self.scan_bt()

    def scan_bt(self):
        self.bt_devs = None
        self.pair, self.pair_proc = None, None
        self.refresh_bt_menu()
        bg(lambda: json.loads(root(SYS_HELPER, "bt-scan", timeout=60)), self.on_bt_scan)

    def on_bt_scan(self, res):
        self.bt_devs = res if isinstance(res, list) else []
        if isinstance(res, Exception):
            self.notify("Bluetooth isn't available on this Pi")
        self.refresh_bt_menu()

    def refresh_bt_menu(self):
        if self.menu and self.menu.get("id") == "bt":
            self.menu["rows"] = self.bt_rows()
            self.menu["sel"] = min(self.menu["sel"], len(self.menu["rows"]) - 1)

    def bt_rows(self):
        rows = []
        if self.bt_devs is None:
            rows.append(row("Searching for devices... (put yours in pairing mode)"))
        for d in self.bt_devs or []:
            status = "Connected" if d["connected"] else "Paired" if d["paired"] else ""
            rows.append(row(d["name"], value="  ·  ".join(x for x in (d["kind"], status) if x),
                            ok=lambda d=d: self.open_bt_device(d)))
        if self.bt_devs == []:
            rows.append(row("No devices found"))
        rows += [row("Search again", ok=self.scan_bt), row("Back", ok=self.back)]
        return rows

    def open_bt_device(self, d):
        rows = []
        if d["connected"]:
            rows.append(row("Disconnect", ok=lambda: self.bt_act("disconnect", d)))
        else:
            rows.append(row("Connect" if d["paired"] else "Pair and connect",
                            ok=lambda: self.bt_act("connect", d) if d["paired"] else self.start_pair(d)))
        if d["paired"]:
            rows.append(row("Forget this device", ok=lambda: self.bt_act("forget", d)))
        rows.append(row("Back", ok=self.back))
        self.open_menu(d["name"], rows, "bt-device")

    def start_pair(self, d):
        """Interactive pairing: the helper streams JSON events (passkey to show, PIN needed...)."""
        self.back()
        self.pair = {"name": d["name"], "kind": "wait", "code": "", "label": ""}
        self.view = "pair"

        def post(ev):
            pygame.event.post(pygame.event.Event(BG_EVENT, cb=self.on_pair_event, res=ev))

        if DEV:
            self.pair_proc = None
            def fake():
                time.sleep(0.6)
                post({"event": "display_passkey", "code": "482915"})
                time.sleep(2.5)
                post({"event": "result", "ok": True, "msg": "Paired and connected (dev)"})
            threading.Thread(target=fake, daemon=True).start()
            return
        proc = subprocess.Popen(["sudo", "-n", SYS_HELPER, "bt-pair", d["mac"]], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.pair_proc = proc

        def read():
            got = False
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                got = got or ev.get("event") == "result"
                post(ev)
            if not got:
                post({"event": "result", "ok": False, "msg": "Pairing failed"})
        threading.Thread(target=read, daemon=True).start()

    def pair_send(self, text):
        p = getattr(self, "pair_proc", None)
        if DEV or not p or not p.stdin:
            print("PAIR ANSWER:", text)
            return
        try:
            p.stdin.write(text + "\n")
            p.stdin.flush()
        except (OSError, ValueError):
            pass

    def pair_cancel(self):
        p = getattr(self, "pair_proc", None)
        if p and p.stdin:
            try:
                p.stdin.close()  # EOF tells the helper to cancel
            except OSError:
                pass
        self.pair = None
        self.view = "menu"
        self.notify("Pairing cancelled")
        self.scan_bt()

    def on_pair_event(self, ev):
        if not getattr(self, "pair", None):
            return  # cancelled; ignore late events
        e = ev.get("event")
        name = self.pair["name"]
        if e in ("display_passkey", "display_pin"):
            self.pair.update(kind="show", code=ev["code"],
                             label=f"Type this code on {name}, then press Enter")
            self.view = "pair"
        elif e == "confirm":
            self.pair.update(kind="confirm", code=ev["code"],
                             label=f"Does {name} show the same code?")
            self.view = "pair"
        elif e in ("request_pin", "request_passkey"):
            self.open_keyboard(f"PIN for {name} (try 0000 or 1234)", "", self.pair_send, cancel=self.pair_cancel)
        elif e == "result":
            self.pair = None
            if self.view in ("pair", "keyboard"):
                self.view = "menu"
            self.notify(ev.get("msg", "Done"))
            self.scan_bt()

    def bt_act(self, action, d):
        self.back()
        self.notify({"pair": "Pairing...", "connect": "Connecting..."}.get(action, "Working..."), 60)
        bg(lambda: json.loads(root(SYS_HELPER, "bt", action, d["mac"], timeout=90)), self.on_bt_done)

    def on_bt_done(self, res):
        self.notify(res.get("msg", "Done") if isinstance(res, dict) else "Bluetooth failed")
        self.scan_bt()

    # ---------- Wi-Fi / hostname ----------
    def open_wifi(self):
        self.open_menu("Wi-Fi", self.wifi_rows(), "wifi")
        self.scan_wifi()

    def scan_wifi(self):
        self.wifi_nets = None
        self.refresh_wifi_menu()
        bg(lambda: json.loads(root(SYS_HELPER, "wifi-list", timeout=60)), self.on_scan)

    def on_scan(self, res):
        self.wifi_nets = res if isinstance(res, list) else []
        if isinstance(res, Exception):
            self.notify("Wi-Fi scan failed (is NetworkManager running?)")
        self.refresh_wifi_menu()

    def refresh_wifi_menu(self):
        if self.menu and self.menu.get("id") == "wifi":
            self.menu["rows"] = self.wifi_rows()
            self.menu["sel"] = min(self.menu["sel"], len(self.menu["rows"]) - 1)

    def wifi_rows(self):
        rows = []
        if self.wifi_nets is None:
            rows.append(row("Scanning..."))
        for n in self.wifi_nets or []:
            tag = ("Connected  ·  " if n["active"] else "") + f"{n['signal']}%  ·  " + (
                "secured" if n["secure"] else "open")
            rows.append(row(n["ssid"], value=tag, ok=lambda n=n: self.connect(n)))
        rows += [row("Rescan", ok=self.scan_wifi), row("Back", ok=self.back)]
        return rows

    def connect(self, n):
        if n["secure"]:
            self.open_keyboard(f"Password for {n['ssid']}", "", lambda pw: self.do_connect(n["ssid"], pw), mask=True)
        else:
            self.do_connect(n["ssid"], "")

    def do_connect(self, ssid, pw):
        self.notify(f"Connecting to {ssid}...", 60)
        bg(lambda: json.loads(root(SYS_HELPER, "wifi-connect", ssid, stdin=pw, timeout=90)), self.on_connected)

    def on_connected(self, res):
        self.notify(res.get("msg", "Done") if isinstance(res, dict) else "Could not connect")
        self.refresh_net()
        self.scan_wifi()

    def edit_hostname(self):
        self.open_keyboard("Device name", socket.gethostname(), self.set_hostname)

    def set_hostname(self, name):
        name = name.strip().lower()
        bg(lambda: json.loads(root(SYS_HELPER, "set-hostname", name)),
           lambda res: self.notify(res.get("msg", "Done") if isinstance(res, dict) else "Could not rename"))

    # ---------- updates ----------
    def open_updates(self):
        rows = [
            row("Operating system", value=lambda: self.upd_label("os"), ok=lambda: self.run_update("os"),
                hint="Press OK to update now"),
            row("Jellyfin Desktop", value=lambda: self.upd_label("jellyfin"),
                ok=lambda: self.run_update("jellyfin"), hint="Press OK to update now"),
            row("Chiaki-ng", value=lambda: self.upd_label("chiaki"),
                ok=lambda: self.run_update("chiaki"), hint="Press OK to update now"),
            row("CouchPi", value=lambda: self.upd_label("couchpi"),
                ok=lambda: self.run_update("couchpi"), hint="Press OK to update now"),
            row("Check for updates", value=self.progress_label, ok=self.run_check),
            row("Update everything now", value=self.progress_label, ok=lambda: self.run_update("all")),
            self.opt("Auto-update system", "auto_os", AUTO_OS_OPTIONS),
            self.opt("Auto-update Jellyfin", "auto_jellyfin", ONOFF),
            self.opt("Auto-update Chiaki-ng", "auto_chiaki", ONOFF),
            self.opt("Auto-update CouchPi", "auto_couchpi", ONOFF),
            self.opt("Auto-update schedule", "auto_frequency", FREQ_OPTIONS),
            row("Back", ok=self.back),
        ]
        self.open_menu("Updates", rows, "updates")

    def run_check(self):
        self._run(["check"], "Checking for updates...", "Check finished")

    def run_update(self, target):
        self._run(["run", target], "Updating...", "Update finished")

    def _run(self, args, start, done):
        if self.busy:
            self.notify("Something is already running")
            return
        self.busy = True
        self.notify(start, 10)
        bg(lambda: root(UPDATER, *args, timeout=3600), lambda res: self._ran(res, done))

    def _ran(self, res, done):
        self.busy = False
        self._status_at = 0
        self.notify("That didn't work - see journalctl -u couchpi" if isinstance(res, Exception) else done)

    # ---------- keyboard ----------
    def open_keyboard(self, title, text, cb, mask=False, cancel=None):
        self.kb = {"title": title, "text": text, "cb": cb, "cancel": cancel, "mask": mask, "show": not mask,
                   "layer": "abc", "shift": False, "r": 1, "c": 0}
        self.view = "keyboard"

    def kb_rows(self):
        return KB_LAYERS[self.kb["layer"]] + [KB_BOTTOM]

    def handle_kb(self, act):
        kb = self.kb
        rows = self.kb_rows()
        if act == "back":
            self.kb_cancel()
        elif act in ("left", "right"):
            kb["c"] = (kb["c"] + (1 if act == "right" else -1)) % len(rows[kb["r"]])
        elif act in ("up", "down"):
            kb["r"] = (kb["r"] + (1 if act == "down" else -1)) % len(rows)
            kb["c"] = min(kb["c"], len(rows[kb["r"]]) - 1)
        elif act == "ok":
            key = rows[kb["r"]][kb["c"]]
            if key == "Shift":
                kb["shift"] = not kb["shift"]
            elif key == "abc/#+=":
                kb["layer"] = "sym" if kb["layer"] == "abc" else "abc"
            elif key == "Show":
                kb["show"] = not kb["show"]
            elif key == "Space":
                kb["text"] += " "
            elif key == "Del":
                kb["text"] = kb["text"][:-1]
            elif key == "Clear":
                kb["text"] = ""
            elif key == "Cancel":
                self.kb_cancel()
            elif key == "Done":
                self.view = "pair" if getattr(self, "pair", None) else "menu"
                kb["cb"](kb["text"])
            else:
                kb["text"] += key.upper() if kb["shift"] and kb["layer"] == "abc" else key

    def type_key(self, ev):
        """Typing on a real keyboard (or the phone companion) fills the on-screen keyboard."""
        if ev.key == pygame.K_BACKSPACE:
            self.kb["text"] = self.kb["text"][:-1]
            return True
        if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and (ev.mod & (pygame.KMOD_SHIFT | pygame.KMOD_CTRL)):
            self.handle_kb_done()
            return True
        if ev.unicode and ev.unicode.isprintable() and ev.key not in (pygame.K_SPACE,) or ev.key == pygame.K_SPACE:
            self.kb["text"] += ev.unicode or " "
            return True
        return False

    def handle_kb_done(self):
        self.view = "pair" if getattr(self, "pair", None) else "menu"
        self.kb["cb"](self.kb["text"])

    def kb_cancel(self):
        self.view = "menu"
        if self.kb.get("cancel"):
            self.kb["cancel"]()

    # ---------- actions / input ----------
    # ---------- phone companion ----------
    def open_companion(self):
        self.refresh_net()
        try:
            with open(os.path.join(RUN_DIR, "companion.json")) as f:
                self.comp = json.load(f)
        except (OSError, ValueError):
            self.comp = {}
        self.view = "companion"

    def qr_surface(self, url, size):
        key = ("qr", url, size)
        if key not in self.icons:
            surf = None
            try:
                import qrcode
                qr = qrcode.QRCode(border=0, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
                qr.add_data(url)
                qr.make(fit=True)
                m = qr.get_matrix()
                cell = max(1, size // (len(m) + 4))
                dim = cell * (len(m) + 4)
                surf = pygame.Surface((dim, dim))
                surf.fill((255, 255, 255))
                for y, line in enumerate(m):
                    for x, dark in enumerate(line):
                        if dark:
                            pygame.draw.rect(surf, (0, 0, 0), ((x + 2) * cell, (y + 2) * cell, cell, cell))
            except Exception:  # qrcode not installed: show the address only
                surf = None
            self.icons[key] = surf
        return self.icons[key]

    def draw_companion(self):
        info = getattr(self, "comp", {})
        self.text(self.f_big, "Phone keyboard", TEXT, midtop=(self.W / 2, self.H * 0.11))
        if not info.get("pin"):
            self.text(self.f_mid, "The phone keyboard service isn't running.", TEXT, center=(self.W / 2, self.H * 0.45))
            self.text(self.f_small, "Check:  sudo systemctl status couchpi-companion", DIM, center=(self.W / 2, self.H * 0.53))
            self.text(self.f_small, "Back = return", DIM, midbottom=(self.W / 2, self.H * 0.93))
            return
        port = info.get("port", 80)
        suffix = "" if port == 80 else f":{port}"
        ip = self.net.get("ip")
        urls = [f"http://{ip}{suffix}"] if ip else []
        urls.append(f"http://{socket.gethostname()}.local{suffix}")
        x = self.W * 0.1
        self.text(self.f_small, "1.  Join the same Wi-Fi as this Pi, then open on your phone:", DIM, topleft=(x, self.H * 0.28))
        for i, u in enumerate(urls):
            self.text(self.f_mid, u, TEXT, topleft=(x + self.W * 0.02, self.H * (0.35 + 0.07 * i)))
        self.text(self.f_small, "2.  Enter this PIN when asked:", DIM, topleft=(x, self.H * 0.55))
        big = pygame.font.Font(self.font_path_bold if hasattr(self, "font_path_bold") else None, int(self.H * 0.16))
        self.text(big, info["pin"], TEXT, topleft=(x + self.W * 0.02, self.H * 0.61))
        qr = self.qr_surface(urls[0], int(self.H * 0.42))
        if qr:
            self.screen.blit(qr, qr.get_rect(center=(self.W * 0.78, self.H * 0.5)))
            self.text(self.f_small, "or scan", DIM, midtop=(self.W * 0.78, self.H * 0.5 + qr.get_height() / 2 + 8))
        self.text(self.f_small, "Back = return", DIM, midbottom=(self.W / 2, self.H * 0.93))

    def launch(self, app):
        cmd = app["command"].format_map(SafeDict(self.cfg))
        if DEV:
            print("LAUNCH:", cmd)
            self.notify(f"(dev) would launch {app['name']}")
            return
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(os.path.join(RUN_DIR, "pointer_mode"), "w") as f:
            f.write("1" if app.get("pointer") else "0")  # remote arrows move a mouse pointer in this app
        with open(os.path.join(RUN_DIR, "launch"), "w") as f:
            f.write(cmd)
        pygame.quit()
        sys.exit(0)

    def handle(self, act):
        if self.view == "home":
            if self.power_focus:
                if act in ("down", "back"):
                    self.power_focus = False
                elif act == "ok":
                    self.open_power_menu()
            elif act == "up":
                self.power_focus = True
            elif act == "left":
                self.sel = max(0, self.sel - 1)
            elif act == "right":
                self.sel = min(len(self.items) - 1, self.sel + 1)
            elif act == "ok":
                item = self.items[self.sel]
                if item.get("_builtin") == "settings":
                    self.open_settings()
                else:
                    self.launch(item)
        elif self.view == "keyboard":
            self.handle_kb(act)
        elif self.view == "companion":
            if act in ("back", "ok"):
                self.view = "menu"
        elif self.view == "pair":
            if act == "back":
                self.pair_cancel()
            elif act == "ok" and self.pair and self.pair["kind"] == "confirm":
                self.pair["kind"], self.pair["label"] = "wait", "Confirming..."
                self.pair_send("yes")
        else:
            m = self.menu
            rows = m["rows"]
            r = rows[m["sel"]]
            if act == "up":
                m["sel"] = (m["sel"] - 1) % len(rows)
            elif act == "down":
                m["sel"] = (m["sel"] + 1) % len(rows)
            elif act == "back":
                self.back()
            elif act in ("left", "right") and r["change"]:
                r["change"](-1 if act == "left" else 1)
            elif act == "ok":
                if r["ok"]:
                    r["ok"]()
                elif r["change"]:
                    r["change"](1)

    # ---------- drawing ----------
    def draw_header(self):
        self.text(self.f_mid, "CouchPi", TEXT, topleft=(self.W * 0.05, self.H * 0.05))
        right = self.W * 0.95
        if self.view == "home":
            d = int(self.H * 0.07)
            cx, cy = int(right - d / 2), int(self.H * 0.08)
            if self.power_focus:
                pygame.draw.circle(self.screen, (70, 80, 130), (cx, cy), d // 2 + 6)
                pygame.draw.circle(self.screen, (235, 238, 250), (cx, cy), d // 2 + 6, 3)
            icon = self.icon_for({"glyph": "power"}, int(d * 0.7))
            icon.set_alpha(255 if self.power_focus else 140)
            self.screen.blit(icon, icon.get_rect(center=(cx, cy)))
            if self.power_focus:
                self.text(self.f_small, "Power", TEXT, midtop=(cx, cy + d // 2 + 12))
            right = cx - d // 2 - self.W * 0.02
        self.text(self.f_mid, time.strftime("%H:%M"), DIM, topright=(right, self.H * 0.05))

    def draw_home(self, dt):
        self.anim += (self.sel - self.anim) * min(1.0, dt * 10)
        tw = int(self.W * 0.17)
        th = int(tw * 0.78)
        gap = int(tw * 0.2)
        cy = self.H * 0.50
        for i, item in enumerate(self.items):
            focus = max(0.0, 1.0 - abs(i - self.anim))
            scale = 1.0 + 0.2 * focus
            w, h = int(tw * scale), int(th * scale)
            cx = self.W / 2 + (i - self.anim) * (tw + gap)
            if cx < -tw or cx > self.W + tw:
                continue
            rect = pygame.Rect(0, 0, w, h)
            rect.center = (cx, cy)
            base = pygame.Color(item.get("color", "#374151"))
            col = mix((base.r, base.g, base.b), (40, 44, 60), 0.55 * (1 - focus))
            if focus > 0.5 and not self.power_focus:
                glow = rect.inflate(int(w * 0.04), int(w * 0.04))
                pygame.draw.rect(self.screen, (235, 238, 250), glow, 3, border_radius=int(h * 0.12))
            pygame.draw.rect(self.screen, col, rect, border_radius=int(h * 0.1))
            icon = self.icon_for(item, int(h * 0.55))
            if icon:
                self.screen.blit(icon, icon.get_rect(center=rect.center))
            else:
                self.text(self.f_big, item["name"][:1].upper(), TEXT, center=rect.center)
            if focus > 0.5:
                self.text(self.f_mid, item["name"], TEXT, midtop=(cx, rect.bottom + self.H * 0.03))
        desc = self.items[self.sel].get("description", "")
        if desc:
            self.text(self.f_small, desc, DIM, midtop=(self.W / 2, self.H * 0.78))

    def draw_menu(self):
        m = self.menu
        self.text(self.f_big, m["title"], TEXT, midtop=(self.W / 2, self.H * 0.11))
        rh, gap = int(self.H * 0.07), int(self.H * 0.01)
        rw = int(self.W * 0.7)
        top = self.H * 0.24
        visible = max(1, int((self.H * 0.88 - top) // (rh + gap)))
        start = max(0, min(m["sel"] - visible // 2, len(m["rows"]) - visible))
        for i in range(start, min(len(m["rows"]), start + visible)):
            r = m["rows"][i]
            rect = pygame.Rect((self.W - rw) // 2, int(top + (i - start) * (rh + gap)), rw, rh)
            sel = i == m["sel"]
            pygame.draw.rect(self.screen, (70, 80, 130) if sel else (24, 28, 46), rect, border_radius=int(rh * 0.25))
            self.text(self.f_small, r["label"], TEXT if sel else DIM, midleft=(rect.left + rh * 0.4, rect.centery))
            v = r["value"]() if callable(r["value"]) else r["value"]
            if v:
                self.text(self.f_small, v, TEXT if sel else DIM, midright=(rect.right - rh * 0.4, rect.centery))
        cx, a = self.W / 2, int(self.H * 0.012)
        if start > 0:
            y = int(top - self.H * 0.025)
            pygame.draw.polygon(self.screen, DIM, [(cx - a, y + a), (cx + a, y + a), (cx, y - a)])
        if start + visible < len(m["rows"]):
            y = int(self.H * 0.9)
            pygame.draw.polygon(self.screen, DIM, [(cx - a, y - a), (cx + a, y - a), (cx, y + a)])
        hint = m["rows"][m["sel"]]["hint"]
        if hint:
            self.text(self.f_small, hint, DIM, midbottom=(self.W / 2, self.H * 0.935))

    def draw_keyboard(self):
        kb = self.kb
        self.text(self.f_big, kb["title"], TEXT, midtop=(self.W / 2, self.H * 0.09))
        box = pygame.Rect(0, 0, int(self.W * 0.7), int(self.H * 0.09))
        box.midtop = (self.W / 2, self.H * 0.2)
        pygame.draw.rect(self.screen, (24, 28, 46), box, border_radius=12)
        shown = kb["text"] if kb["show"] else "•" * len(kb["text"])
        cursor = "_" if int(time.time() * 2) % 2 == 0 else " "
        self.text(self.f_mid, shown + cursor, TEXT, midleft=(box.left + 20, box.centery))
        kw, kh, gap = int(self.W * 0.065), int(self.H * 0.09), int(self.W * 0.008)
        top = self.H * 0.34
        for r, keys in enumerate(self.kb_rows()):
            wide = [(int(kw * 1.45) if len(k) > 1 else kw) for k in keys]
            x = (self.W - (sum(wide) + gap * (len(keys) - 1))) / 2
            for c, key in enumerate(keys):
                rect = pygame.Rect(int(x), int(top + r * (kh + gap)), wide[c], kh)
                x += wide[c] + gap
                sel = (r, c) == (kb["r"], kb["c"])
                on = (key == "Shift" and kb["shift"]) or (key == "Show" and kb["show"] and kb["mask"])
                pygame.draw.rect(self.screen, (70, 80, 130) if sel else (60, 90, 70) if on else (24, 28, 46),
                                 rect, border_radius=10)
                label = key.upper() if (kb["shift"] and kb["layer"] == "abc" and len(key) == 1) else key
                self.text(self.f_small, label, TEXT if sel else DIM, center=rect.center)
        self.text(self.f_small, "Back = cancel", DIM, midbottom=(self.W / 2, self.H * 0.93))

    def draw_pair(self):
        p = self.pair or {}
        self.text(self.f_big, f"Pairing {p.get('name', '')}", TEXT, midtop=(self.W / 2, self.H * 0.14))
        if p.get("code"):
            big = pygame.font.Font(None, int(self.H * 0.22))
            self.text(big, p["code"], TEXT, center=(self.W / 2, self.H * 0.48))
        self.text(self.f_mid, p.get("label") or "Waiting for the device...", DIM, center=(self.W / 2, self.H * 0.68))
        hint = "OK = yes     Back = cancel" if p.get("kind") == "confirm" else "Back = cancel"
        self.text(self.f_small, hint, DIM, midbottom=(self.W / 2, self.H * 0.93))

    def draw(self, dt):
        surf = pygame.display.get_surface()
        if surf is not None and surf.get_size() != (self.W, self.H):
            self.screen = surf  # window was resized by the compositor
            self.layout()
        self.screen.blit(self.bg, (0, 0))
        self.draw_header()
        if self.view == "home":
            self.draw_home(dt)
        elif self.view == "companion":
            self.draw_companion()
        elif self.view == "pair":
            self.draw_pair()
        elif self.view == "keyboard":
            self.draw_keyboard()
        else:
            self.draw_menu()
        if time.time() < self.message_until:
            self.text(self.f_small, self.message, TEXT, midbottom=(self.W / 2, self.H * 0.98))
        pygame.display.flip()

    # ---------- main loop ----------
    def run(self):
        clock = pygame.time.Clock()
        while True:
            dt = clock.tick(60) / 1000
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == BG_EVENT:
                    ev.cb(ev.res)
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_q and DEV and self.view != "keyboard":
                        return
                    if self.view == "keyboard" and self.kb and self.type_key(ev):
                        continue
                    act = KEYMAP.get(ev.key)
                    if act:
                        self.handle(act)
                elif ev.type == pygame.JOYHATMOTION:
                    act = {(-1, 0): "left", (1, 0): "right", (0, 1): "up", (0, -1): "down"}.get(tuple(ev.value))
                    if act:
                        self.handle(act)
                elif ev.type == pygame.JOYBUTTONDOWN:
                    if ev.button == 0:
                        self.handle("ok")
                    elif ev.button == 1:
                        self.handle("back")
            self.draw(dt)


if __name__ == "__main__":
    Launcher().run()
