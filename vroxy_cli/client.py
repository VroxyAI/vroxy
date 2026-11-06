"""HTTP client for /api/mobile/v1.

Stdlib only, on purpose: the install path is `curl | bash` onto a box
that may have nothing but python3, and a dependency is one more thing
to be wrong at 3am.

The API is called "mobile" because the phone shipped first. It is the
operator API: the tenant comes from /workspaces/:hashid/... resolved
through the caller's own memberships, and every action is gated on
their capability. The CLI is a client, not a new permission surface.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "https://vroxy.ai"
API_BASE = "/api/mobile/v1"
TIMEOUT = 30
# Cloudflare's bot score bans Python-urllib's default UA (error 1010).
# Identify as the vroxy CLI so the zone can allowlist us without
# pretending to be a browser.
USER_AGENT = f"vroxy-cli/{__import__('vroxy_cli.version', fromlist=['VERSION']).VERSION} (+https://vroxy.ai)"
CLIENT_ID = f"vroxy-cli/{__import__('vroxy_cli.version', fromlist=['VERSION']).VERSION}"
LATEST_HEADER = "X-Vroxy-Client-Latest"
NOTICE_INTERVAL_SECONDS = 24 * 60 * 60


class VroxyError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class Client:
    def __init__(self, host=None, token=None, mobile_token=None):
        self.host = (host or os.environ.get("VROXY_HOST") or DEFAULT_HOST).rstrip("/")
        self.token = token
        self.mobile_token = mobile_token

    def _request(self, method, path, body=None, authed=True, mobile=False):
        url = f"{self.host}{API_BASE}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("X-Vroxy-Client", CLIENT_ID)
        if authed:
            use = self.mobile_token if mobile else self.token
            if not use:
                hint = "vroxy login --mobile" if mobile else "vroxy login"
                raise VroxyError(f"Not signed in. Run: {hint}")
            req.add_header("Authorization", f"Bearer {use}")

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode()
                _notice_quietly(resp)
        except urllib.error.HTTPError as e:
            _notice_quietly(e)
            raw = e.read().decode()
            payload = _safe_json(raw)
            message = payload.get("error") or f"HTTP {e.code}"
            if e.code == 401:
                message = "Token rejected. Run: vroxy login"
            raise VroxyError(message, status=e.code, code=payload.get("code")) from None
        except urllib.error.URLError as e:
            raise VroxyError(f"Could not reach {self.host}: {e.reason}") from None

        return _safe_json(raw)

    def device_authorize(self, mobile=False, device=None):
        body = {"client": "mobile" if mobile else "cli"}
        if device:
            body["device_model"] = device
        return self._request("POST", "/device/authorize", body, authed=False)

    def device_token(self, device_code, code):
        body = {"device_code": device_code, "code": code}
        return self._request("POST", "/device/token", body, authed=False)

    def logout(self):
        return self._request("POST", "/logout")

    def me(self):
        return self._request("GET", "/me")

    def workspaces(self, mobile=False):
        return self._request("GET", "/workspaces", mobile=mobile).get("workspaces", [])

    def _resolve_workspace(self, workspace, mobile=False):
        if not workspace:
            return workspace
        key = str(workspace)
        rows = self.workspaces(mobile=mobile)
        for w in rows:
            if w.get("hashid") == key:
                return key
        for w in rows:
            if w.get("name") == key:
                return w.get("hashid")
        folded = key.casefold()
        matches = [w for w in rows if (w.get("name") or "").casefold() == folded]
        if len(matches) == 1:
            return matches[0].get("hashid")
        if len(matches) > 1:
            names = ", ".join(sorted(w.get("name") or "?" for w in matches))
            raise VroxyError(f"'{key}' matches {len(matches)} workspaces ({names}) — pass a hashid")
        return key

    def rooms(self, workspace):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/rooms").get("rooms", [])

    def messages(self, workspace, room, limit=20):
        limit = int(limit)
        workspace = self._resolve_workspace(workspace)
        payload = self._request(
            "GET", f"/workspaces/{workspace}/rooms/{room}/messages?limit={limit}"
        )
        return payload.get("messages", [])

    def post_message(self, workspace, room, body):
        workspace = self._resolve_workspace(workspace)
        return self._request(
            "POST", f"/workspaces/{workspace}/rooms/{room}/messages", {"body": body}
        )

    def dispatch_status(self, workspace):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/dispatch_status")

    def agents(self, workspace):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/dispatch")

    def remove_agent(self, workspace, agent):
        workspace = self._resolve_workspace(workspace, mobile=True)
        return self._request("DELETE", f"/workspaces/{workspace}/dispatch/{agent}", mobile=True)

    def docs(self, workspace, status=None, q=None):
        workspace = self._resolve_workspace(workspace)
        query = _query({"status": status, "q": q})
        return self._request("GET", f"/workspaces/{workspace}/docs{query}").get("docs", [])

    def doc(self, workspace, doc):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/docs/{doc}").get("doc", {})

    def create_doc(self, workspace, **fields):
        workspace = self._resolve_workspace(workspace)
        payload = self._request("POST", f"/workspaces/{workspace}/docs", {"doc": _compact(fields)})
        return payload.get("doc", {})

    def update_doc(self, workspace, doc, **fields):
        workspace = self._resolve_workspace(workspace)
        payload = self._request(
            "PATCH", f"/workspaces/{workspace}/docs/{doc}", {"doc": _compact(fields)}
        )
        return payload.get("doc", {})

    def publish_doc(self, workspace, doc, published=True):
        workspace = self._resolve_workspace(workspace)
        verb = "publish" if published else "unpublish"
        payload = self._request("POST", f"/workspaces/{workspace}/docs/{doc}/{verb}")
        return payload.get("doc", {})

    def delete_doc(self, workspace, doc):
        workspace = self._resolve_workspace(workspace)
        return self._request("DELETE", f"/workspaces/{workspace}/docs/{doc}")

    def members(self, workspace):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/members")

    def tools(self, workspace):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/tools").get("tools", [])

    def tool(self, workspace, tool):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/tools/{tool}").get("tool", {})

    def create_tool(self, workspace, **fields):
        workspace = self._resolve_workspace(workspace)
        payload = self._request("POST", f"/workspaces/{workspace}/tools", {"tool": _compact(fields)})
        return payload.get("tool", {})

    def update_tool(self, workspace, tool, **fields):
        workspace = self._resolve_workspace(workspace)
        payload = self._request(
            "PATCH", f"/workspaces/{workspace}/tools/{tool}", {"tool": _compact(fields)}
        )
        return payload.get("tool", {})

    def toggle_tool(self, workspace, tool, enabled=None):
        workspace = self._resolve_workspace(workspace)
        body = None if enabled is None else {"enabled": bool(enabled)}
        payload = self._request("POST", f"/workspaces/{workspace}/tools/{tool}/toggle", body)
        return payload.get("tool", {})

    def delete_tool(self, workspace, tool):
        workspace = self._resolve_workspace(workspace)
        return self._request("DELETE", f"/workspaces/{workspace}/tools/{tool}")

    def search_rooms(self, workspace, q, limit=20):
        workspace = self._resolve_workspace(workspace)
        query = _query({"q": q, "limit": limit})
        return self._request(
            "GET", f"/workspaces/{workspace}/rooms/search{query}"
        ).get("results", [])

    def chats(self, workspace, filter="open", q=None, page=None):
        workspace = self._resolve_workspace(workspace)
        query = _query({"filter": filter, "q": q, "page": page})
        return self._request("GET", f"/workspaces/{workspace}/chats{query}")

    def chat(self, workspace, chat):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/chats/{chat}")

    def errors(self, workspace, source=None, page=None):
        workspace = self._resolve_workspace(workspace)
        query = _query({"source": source, "page": page})
        return self._request("GET", f"/workspaces/{workspace}/errors{query}")

    def error(self, workspace, fingerprint, page=None):
        workspace = self._resolve_workspace(workspace)
        query = _query({"page": page})
        path = urllib.parse.quote(str(fingerprint), safe="")
        return self._request("GET", f"/workspaces/{workspace}/errors/{path}{query}")

    def usage(self, workspace):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/usage")

    def visitors(self, workspace, identity=None, active=False, page=None):
        workspace = self._resolve_workspace(workspace)
        query = _query({"identity": identity, "active": "yes" if active else None, "page": page})
        return self._request("GET", f"/workspaces/{workspace}/visitors{query}")

    def visitor(self, workspace, visitor):
        workspace = self._resolve_workspace(workspace)
        return self._request("GET", f"/workspaces/{workspace}/visitors/{visitor}")

    def targets(self, workspace, page=None):
        workspace = self._resolve_workspace(workspace)
        query = _query({"page": page})
        return self._request("GET", f"/workspaces/{workspace}/dispatch_targets{query}")

    # The writes below need a `mobile`-scoped token — one a person
    # mints with `vroxy login --mobile` — because the agent token is
    # refused for workspace/room/member management by design.

    def create_workspace(self, name):
        return self._request("POST", "/workspaces", {"name": name}, mobile=True)

    def rename_workspace(self, workspace, name=None, slug=None):
        workspace = self._resolve_workspace(workspace, mobile=True)
        return self._request(
            "PATCH", f"/workspaces/{workspace}", _compact({"name": name, "slug": slug}),
            mobile=True
        )

    def create_room(self, workspace, name, topic=None):
        workspace = self._resolve_workspace(workspace, mobile=True)
        return self._request(
            "POST", f"/workspaces/{workspace}/rooms",
            _compact({"name": name, "topic": topic}), mobile=True
        )

    def invite_member(self, workspace, email, role):
        workspace = self._resolve_workspace(workspace, mobile=True)
        return self._request(
            "POST", f"/workspaces/{workspace}/members", {"email": email, "role": role},
            mobile=True
        )

    def update_member(self, workspace, membership_id, role):
        workspace = self._resolve_workspace(workspace, mobile=True)
        return self._request(
            "PATCH", f"/workspaces/{workspace}/members/{membership_id}", {"role": role},
            mobile=True
        )

    def remove_member(self, workspace, membership_id):
        workspace = self._resolve_workspace(workspace, mobile=True)
        return self._request(
            "DELETE", f"/workspaces/{workspace}/members/{membership_id}", mobile=True
        )

    def revoke_invite(self, workspace, invitation_hashid):
        workspace = self._resolve_workspace(workspace, mobile=True)
        return self._request(
            "DELETE", f"/workspaces/{workspace}/members/invitations/{invitation_hashid}",
            mobile=True
        )


def _header(response, name):
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name)
    except AttributeError:
        return None


def _notice_quietly(response):
    try:
        announce_if_newer(_header(response, LATEST_HEADER))
    except Exception:
        return


def _version_tuple(text):
    parts = str(text or "").strip().split(".")
    if len(parts) != 3 or not all(p.isascii() and p.isdigit() for p in parts):
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def announce_if_newer(latest, now=None, stream=None):
    from . import config
    from .version import VERSION

    theirs, ours = _version_tuple(latest), _version_tuple(VERSION)
    if not theirs or not ours or theirs <= ours:
        return False
    now = time.time() if now is None else now
    try:
        shown = float(config.load_notice().get("shown_at") or 0)
    except (TypeError, ValueError):
        shown = 0
    if now - shown < NOTICE_INTERVAL_SECONDS:
        return False
    try:
        config.save_notice({"shown_at": now, "latest": str(latest).strip()})
    except OSError:
        return False
    print(
        f"vroxy {str(latest).strip()} is available (you have {VERSION}) "
        f"— update the vroxy_dispatch checkout",
        file=stream or sys.stderr,
    )
    return True


def _compact(fields):
    return {k: v for k, v in fields.items() if v is not None}


def _query(pairs):
    live = {k: v for k, v in pairs.items() if v}
    return f"?{urllib.parse.urlencode(live)}" if live else ""


def _safe_json(raw):
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {"data": parsed}
