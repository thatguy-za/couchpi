#!/usr/bin/env python3
"""CouchPi phone companion: a small web server that turns a phone into a keyboard,
remote and touchpad for the Pi. Open http://<pi>:8765 on the same Wi-Fi and enter the
PIN shown in CouchPi > Settings > Phone keyboard.

Runs as the unprivileged `couchpi` user (it has access to /dev/uinput through the
`input` group). Standard library only, apart from python3-evdev.
"""
import http.server
import json
import os
import secrets
import signal
import socket
import threading
import time

PORT = int(os.environ.get("COUCHPI_COMPANION_PORT", "8765"))
RUN_DIR = os.environ.get("COUCHPI_RUN", "/run/couchpi")
STATE_DIR = os.environ.get("COUCHPI_STATE", "/var/lib/couchpi")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
DRY = bool(os.environ.get("COUCHPI_DEV"))   # no uinput: just record events (for testing)
MAX_BODY = 20000
MAX_TEXT = 4000

# ---- US-layout key tables (names resolved against evdev.ecodes) ----
UNSHIFTED = {c: f"KEY_{c.upper()}" for c in "abcdefghijklmnopqrstuvwxyz0123456789"}
UNSHIFTED.update({" ": "KEY_SPACE", "-": "KEY_MINUS", "=": "KEY_EQUAL", "[": "KEY_LEFTBRACE",
                  "]": "KEY_RIGHTBRACE", "\\": "KEY_BACKSLASH", ";": "KEY_SEMICOLON",
                  "'": "KEY_APOSTROPHE", ",": "KEY_COMMA", ".": "KEY_DOT", "/": "KEY_SLASH",
                  "`": "KEY_GRAVE", "\n": "KEY_ENTER", "\t": "KEY_TAB"})
SHIFTED = {"_": "KEY_MINUS", "+": "KEY_EQUAL", "{": "KEY_LEFTBRACE", "}": "KEY_RIGHTBRACE",
           "|": "KEY_BACKSLASH", ":": "KEY_SEMICOLON", '"': "KEY_APOSTROPHE", "<": "KEY_COMMA",
           ">": "KEY_DOT", "?": "KEY_SLASH", "~": "KEY_GRAVE", "!": "KEY_1", "@": "KEY_2",
           "#": "KEY_3", "$": "KEY_4", "%": "KEY_5", "^": "KEY_6", "&": "KEY_7", "*": "KEY_8",
           "(": "KEY_9", ")": "KEY_0"}
NAMED = {"enter": "KEY_ENTER", "backspace": "KEY_BACKSPACE", "tab": "KEY_TAB", "esc": "KEY_ESC",
         "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
         "space": "KEY_SPACE", "delete": "KEY_DELETE", "home": "KEY_HOME", "end": "KEY_END",
         "pageup": "KEY_PAGEUP", "pagedown": "KEY_PAGEDOWN", "playpause": "KEY_PLAYPAUSE",
         "menu": "KEY_MENU"}


class Devices:
    """Virtual keyboard + mouse. In DRY mode events are recorded in self.log."""

    def __init__(self):
        self.lock = threading.Lock()
        self.log = []
        self.kb = self.mouse = self.e = None
        if DRY:
            return
        from evdev import UInput, ecodes as e
        self.e = e
        names = set(UNSHIFTED.values()) | set(SHIFTED.values()) | set(NAMED.values()) | {"KEY_LEFTSHIFT"}
        self.kb = UInput({e.EV_KEY: [getattr(e, n) for n in names]}, name="couchpi-phone-keyboard")
        self.mouse = UInput({e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
                             e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT]}, name="couchpi-phone-mouse")

    def _key(self, name, shift=False):
        if DRY:
            self.log.append(("key", name, shift))
            return
        e = self.e
        if shift:
            self.kb.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
        self.kb.write(e.EV_KEY, getattr(e, name), 1)
        self.kb.syn()
        self.kb.write(e.EV_KEY, getattr(e, name), 0)
        if shift:
            self.kb.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
        self.kb.syn()

    def key(self, name, shift=False):
        if name not in NAMED:
            return False
        with self.lock:
            self._key(NAMED[name], shift)
        return True

    def text(self, text):
        """Type text; returns how many characters could not be typed (non-US-keyboard)."""
        skipped = 0
        for ch in text[:MAX_TEXT]:
            with self.lock:
                if ch in UNSHIFTED:
                    self._key(UNSHIFTED[ch])
                elif ch in SHIFTED:
                    self._key(SHIFTED[ch], True)
                elif ch.isascii() and ch.isalpha():
                    self._key(UNSHIFTED[ch.lower()], True)
                else:
                    skipped += 1
                    continue
            time.sleep(0.004)
        return skipped

    def move(self, dx, dy):
        with self.lock:
            if DRY:
                self.log.append(("move", dx, dy))
                return
            e = self.e
            if dx:
                self.mouse.write(e.EV_REL, e.REL_X, dx)
            if dy:
                self.mouse.write(e.EV_REL, e.REL_Y, dy)
            self.mouse.syn()

    def click(self, button):
        with self.lock:
            if DRY:
                self.log.append(("click", button))
                return
            e = self.e
            code = e.BTN_RIGHT if button == "right" else e.BTN_LEFT
            self.mouse.write(e.EV_KEY, code, 1)
            self.mouse.syn()
            time.sleep(0.03)
            self.mouse.write(e.EV_KEY, code, 0)
            self.mouse.syn()

    def scroll(self, dy):
        with self.lock:
            if DRY:
                self.log.append(("scroll", dy))
                return
            self.mouse.write(self.e.EV_REL, self.e.REL_WHEEL, dy)
            self.mouse.syn()


class State:
    """PIN and paired-phone tokens, kept across restarts."""

    def __init__(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.path = os.path.join(STATE_DIR, "companion.json")
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        if not self.data.get("pin"):
            self.data["pin"] = f"{secrets.randbelow(10000):04d}"
        self.data.setdefault("tokens", [])
        self.fails, self.locked_until = 0, 0.0
        self.save()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    def try_pin(self, pin):
        """Returns a new token, or None. Five wrong PINs lock logins for a minute."""
        if time.time() < self.locked_until:
            return None
        if secrets.compare_digest(str(pin), self.data["pin"]):
            self.fails = 0
            token = secrets.token_urlsafe(24)
            self.data["tokens"] = (self.data["tokens"] + [token])[-20:]
            self.save()
            return token
        self.fails += 1
        if self.fails >= 5:
            self.fails, self.locked_until = 0, time.time() + 60
        return None

    def valid(self, token):
        return bool(token) and any(secrets.compare_digest(token, t) for t in self.data["tokens"])


STATE = None
DEVICES = None


def write_status():
    """Tell the launcher how to reach us (it shows the PIN and a QR code)."""
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        tmp = os.path.join(RUN_DIR, "companion.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"port": PORT, "pin": STATE.data["pin"]}, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(RUN_DIR, "companion.json"))
    except OSError:
        pass


def set_pointer(on):
    try:
        with open(os.path.join(RUN_DIR, "pointer_mode"), "w") as f:
            f.write("1" if on else "0")
    except OSError:
        pass


def pointer_on():
    try:
        with open(os.path.join(RUN_DIR, "pointer_mode")) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def kill_app():
    try:
        with open(os.path.join(RUN_DIR, "app.pid")) as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)
        return True
    except (OSError, ValueError):
        return False


def clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "CouchPi"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cp":
                return v
        return ""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/status":
            ok = STATE.valid(self._token())
            self._send(200 if ok else 401, {"ok": ok, "pointer": pointer_on()} if ok else {"ok": False})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_BODY:
                return self._send(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, OSError):
            return self._send(400, {"error": "bad request"})

        if path == "/api/login":
            token = STATE.try_pin(body.get("pin", ""))
            if not token:
                return self._send(401, {"ok": False, "error": "Wrong PIN (5 wrong tries locks it for a minute)"})
            cookie = f"cp={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
            return self._send(200, {"ok": True}, headers=[("Set-Cookie", cookie)])

        if not STATE.valid(self._token()):
            return self._send(401, {"ok": False, "error": "not paired"})
        try:
            if path == "/api/text":
                skipped = DEVICES.text(str(body.get("text", "")))
                return self._send(200, {"ok": True, "skipped": skipped})
            if path == "/api/key":
                ok = DEVICES.key(str(body.get("name", "")), bool(body.get("shift")))
                return self._send(200 if ok else 400, {"ok": ok})
            if path == "/api/move":
                DEVICES.move(clamp(body.get("dx", 0), -300, 300), clamp(body.get("dy", 0), -300, 300))
                return self._send(200, {"ok": True})
            if path == "/api/click":
                DEVICES.click("right" if body.get("button") == "right" else "left")
                return self._send(200, {"ok": True})
            if path == "/api/scroll":
                DEVICES.scroll(clamp(body.get("dy", 0), -20, 20))
                return self._send(200, {"ok": True})
            if path == "/api/pointer":
                on = bool(body.get("on"))
                set_pointer(on)
                return self._send(200, {"ok": True, "pointer": on})
            if path == "/api/home":
                return self._send(200, {"ok": kill_app()})
        except (TypeError, ValueError):
            return self._send(400, {"error": "bad request"})
        self._send(404, {"error": "not found"})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global STATE, DEVICES
    STATE = State()
    DEVICES = Devices()
    write_status()
    print(f"couchpi-companion listening on :{PORT} (host {socket.gethostname()})", flush=True)
    Server(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
