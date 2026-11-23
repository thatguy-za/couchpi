"""Turns held remote direction keys into smooth, accelerating mouse-pointer movement.

Kept free of any evdev import so it can be tested anywhere: the caller supplies
`move(dx, dy)` and reads nothing else. Remote keys may only send "pressed" repeats
and no reliable "released", so a key that has not been seen for STALE seconds stops.
"""
import threading
import time

DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
TICK = 1 / 60
FIRST_STEP = 10      # pixels moved immediately on a tap
BASE_SPEED = 5       # pixels per tick when a key is first held
MAX_SPEED = 28       # ... after accelerating
ACCEL = 20           # extra pixels per tick, per second held
STALE = 0.45         # seconds without a press/repeat before we assume the key is up


class Pointer:
    def __init__(self, move, clock=time.monotonic):
        self.move = move
        self.clock = clock
        self.held = {}  # direction -> [first_seen, last_seen]
        self.lock = threading.Lock()
        self._thread = None

    def press(self, direction):
        now = self.clock()
        with self.lock:
            if direction in self.held:
                self.held[direction][1] = now
                return
            self.held[direction] = [now, now]
        dx, dy = DIRECTIONS[direction]
        self.move(dx * FIRST_STEP, dy * FIRST_STEP)
        self._ensure_thread()

    def release(self, direction):
        with self.lock:
            self.held.pop(direction, None)

    def clear(self):
        with self.lock:
            self.held.clear()

    def step(self):
        """One animation tick; returns the (dx, dy) that was applied."""
        now = self.clock()
        dx = dy = 0
        with self.lock:
            for d in list(self.held):
                start, last = self.held[d]
                if now - last > STALE:
                    del self.held[d]
                    continue
                speed = min(MAX_SPEED, BASE_SPEED + (now - start) * ACCEL)
                vx, vy = DIRECTIONS[d]
                dx += vx * speed
                dy += vy * speed
        dx, dy = int(round(dx)), int(round(dy))
        if dx or dy:
            self.move(dx, dy)
        return dx, dy

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            time.sleep(TICK)
            with self.lock:
                if not self.held:
                    return
            self.step()
