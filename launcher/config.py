"""Shared config for CouchPi (launcher, CEC daemon, updater). Stored as JSON."""
import json
import os

PATH = os.environ.get("COUCHPI_CONFIG", "/etc/couchpi/config.json")

DEFAULTS = {
    "resolution": "auto",         # "auto" or "WxH@Hz" (applied at next boot)
    "sleep_mode": "idle",         # off | idle | shutdown
    "sleep_minutes": 30,          # shutdown delay after TV goes to standby
    "cec_enabled": True,
    "jellyfin_url": "",           # optional; apps can use {jellyfin_url} in their command
    "auto_os": "security",        # off | security | all
    "auto_jellyfin": False,
    "auto_chiaki": False,
    "auto_couchpi": False,
    "auto_frequency": "daily",    # daily | weekly
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, PATH)
