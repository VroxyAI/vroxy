"""Where the token lives.

`~/.config/vroxy/credentials.json`, mode 0600. The token is a bearer
credential for a workspace — anyone who can read the file can act as
you, so the mode is set before anything is written to it, not after.
"""

import json
import os
import stat
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("VROXY_CONFIG_DIR") or Path.home() / ".config" / "vroxy")
CREDENTIALS = CONFIG_DIR / "credentials.json"


def load():
    try:
        return json.loads(CREDENTIALS.read_text())
    except (OSError, ValueError):
        return {}


def save(host, token, email=None):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Create empty and restrict BEFORE writing: a token written first
    # and chmod'd second is world-readable for the gap between.
    CREDENTIALS.touch(mode=0o600, exist_ok=True)
    os.chmod(CREDENTIALS, stat.S_IRUSR | stat.S_IWUSR)
    CREDENTIALS.write_text(
        json.dumps({"host": host, "token": token, "email": email}, indent=2) + "\n"
    )
    return CREDENTIALS


def clear():
    try:
        CREDENTIALS.unlink()
        return True
    except OSError:
        return False


def token_for(host=None):
    saved = load()
    if host and saved.get("host") and saved["host"].rstrip("/") != host.rstrip("/"):
        return None
    return saved.get("token")
