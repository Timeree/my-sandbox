import json
import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from . import tunnel, wsproto

DIR = Path(os.environ.get("MYBOX_CONFIG_DIR") or Path.home() / ".config" / "mybox")
FILE = DIR / "boxes.json"
PATH = "/my-sandbox"


def normalize(url):
    """Give bare urls a scheme and the exact /my-sandbox path marker."""
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or PATH
    return urlunparse(parsed._replace(path=path))


def load():
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def save(store):
    DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(store, indent=2) + "\n")
    os.chmod(FILE, 0o600)


def next_name(store):
    index = 1
    while f"box{index}" in store:
        index += 1
    return f"box{index}"


def probe(url):
    """Open a WebSocket to the box and close it; raises if the gateway refuses."""
    host, port, path, tls = tunnel.parse(url)
    sock = wsproto.connect(host, port, path, tls)
    sock.close()
