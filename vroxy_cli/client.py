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
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "https://vroxy.ai"
API_BASE = "/api/mobile/v1"
TIMEOUT = 30


class VroxyError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class Client:
    def __init__(self, host=None, token=None):
        self.host = (host or os.environ.get("VROXY_HOST") or DEFAULT_HOST).rstrip("/")
        self.token = token

    def _request(self, method, path, body=None, authed=True):
        url = f"{self.host}{API_BASE}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if authed:
            if not self.token:
                raise VroxyError("Not signed in. Run: vroxy login")
            req.add_header("Authorization", f"Bearer {self.token}")

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            payload = _safe_json(raw)
            message = payload.get("error") or f"HTTP {e.code}"
            if e.code == 401:
                message = "Token rejected. Run: vroxy login"
            raise VroxyError(message, status=e.code, code=payload.get("code")) from None
        except urllib.error.URLError as e:
            raise VroxyError(f"Could not reach {self.host}: {e.reason}") from None

        return _safe_json(raw)

    def login(self, email, password):
        return self._request(
            "POST", "/login", {"email": email, "password": password}, authed=False
        )

    def me(self):
        return self._request("GET", "/me")

    def workspaces(self):
        return self._request("GET", "/workspaces").get("workspaces", [])

    def rooms(self, workspace):
        return self._request("GET", f"/workspaces/{workspace}/rooms").get("rooms", [])

    def messages(self, workspace, room, limit=20):
        payload = self._request(
            "GET", f"/workspaces/{workspace}/rooms/{room}/messages?limit={int(limit)}"
        )
        return payload.get("messages", [])

    def post_message(self, workspace, room, body):
        return self._request(
            "POST", f"/workspaces/{workspace}/rooms/{room}/messages", {"body": body}
        )

    def dispatch_status(self, workspace):
        return self._request("GET", f"/workspaces/{workspace}/dispatch_status")

    def docs(self, workspace, status=None, q=None):
        query = _query({"status": status, "q": q})
        return self._request("GET", f"/workspaces/{workspace}/docs{query}").get("docs", [])

    def doc(self, workspace, doc):
        return self._request("GET", f"/workspaces/{workspace}/docs/{doc}").get("doc", {})

    def create_doc(self, workspace, **fields):
        payload = self._request("POST", f"/workspaces/{workspace}/docs", {"doc": _compact(fields)})
        return payload.get("doc", {})

    def update_doc(self, workspace, doc, **fields):
        payload = self._request(
            "PATCH", f"/workspaces/{workspace}/docs/{doc}", {"doc": _compact(fields)}
        )
        return payload.get("doc", {})

    def publish_doc(self, workspace, doc, published=True):
        verb = "publish" if published else "unpublish"
        payload = self._request("POST", f"/workspaces/{workspace}/docs/{doc}/{verb}")
        return payload.get("doc", {})

    def delete_doc(self, workspace, doc):
        return self._request("DELETE", f"/workspaces/{workspace}/docs/{doc}")

    def members(self, workspace):
        return self._request("GET", f"/workspaces/{workspace}/members")

    def invite_member(self, workspace, email, role):
        payload = self._request(
            "POST", f"/workspaces/{workspace}/members", {"email": email, "role": role}
        )
        return payload.get("invitation", {})

    def set_member_role(self, workspace, membership_id, role):
        payload = self._request(
            "PATCH", f"/workspaces/{workspace}/members/{membership_id}", {"role": role}
        )
        return payload.get("member", {})

    def remove_member(self, workspace, membership_id):
        return self._request("DELETE", f"/workspaces/{workspace}/members/{membership_id}")

    def revoke_invitation(self, workspace, invitation):
        return self._request(
            "DELETE", f"/workspaces/{workspace}/members/invitations/{invitation}"
        )

    def tools(self, workspace):
        return self._request("GET", f"/workspaces/{workspace}/tools").get("tools", [])

    def tool(self, workspace, tool):
        return self._request("GET", f"/workspaces/{workspace}/tools/{tool}").get("tool", {})

    def create_tool(self, workspace, **fields):
        payload = self._request("POST", f"/workspaces/{workspace}/tools", {"tool": _compact(fields)})
        return payload.get("tool", {})

    def update_tool(self, workspace, tool, **fields):
        payload = self._request(
            "PATCH", f"/workspaces/{workspace}/tools/{tool}", {"tool": _compact(fields)}
        )
        return payload.get("tool", {})

    def toggle_tool(self, workspace, tool, enabled=None):
        body = None if enabled is None else {"enabled": bool(enabled)}
        payload = self._request("POST", f"/workspaces/{workspace}/tools/{tool}/toggle", body)
        return payload.get("tool", {})

    def delete_tool(self, workspace, tool):
        return self._request("DELETE", f"/workspaces/{workspace}/tools/{tool}")


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
