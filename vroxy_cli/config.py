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


def save(host, token=None, email=None, mobile_token=None):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Create empty and restrict BEFORE writing: a token written first
    # and chmod'd second is world-readable for the gap between.
    CREDENTIALS.touch(mode=0o600, exist_ok=True)
    os.chmod(CREDENTIALS, stat.S_IRUSR | stat.S_IWUSR)
    data = load()
    data["host"] = host
    if token is not None:
        data["token"] = token
    if email is not None:
        data["email"] = email
    if mobile_token is not None:
        data["mobile_token"] = mobile_token
    CREDENTIALS.write_text(json.dumps(data, indent=2) + "\n")
    return CREDENTIALS


def notice_path():
    return CONFIG_DIR / "update-notice.json"


def load_notice():
    try:
        state = json.loads(notice_path().read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_notice(state):
    path = notice_path()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    path.write_text(json.dumps(state) + "\n")
    return path


def clear():
    try:
        CREDENTIALS.unlink()
        return True
    except OSError:
        return False


def token_for(host=None, mobile=False):
    saved = load()
    if host and saved.get("host") and saved["host"].rstrip("/") != host.rstrip("/"):
        return None
    return saved.get("mobile_token" if mobile else "token")
