import argparse
import getpass
import json
import os
import sys

from . import config
from .client import Client, VroxyError
from .version import VERSION


ROLES = ["operator", "member", "admin", "owner"]


def _client(args):
    host = getattr(args, "host", None) or os.environ.get("VROXY_HOST") or config.load().get("host")
    token = os.environ.get("VROXY_TOKEN") or config.token_for(host)
    return Client(host=host, token=token)


def _emit(args, rows, plain):
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
    else:
        plain()


def cmd_login(args):
    host = args.host or os.environ.get("VROXY_HOST") or "https://vroxy.ai"
    email = args.email or input("Email: ").strip()
    # Never accepted as an argument: argv is world-readable via /proc
    # and lands in shell history.
    password = os.environ.get("VROXY_PASSWORD") or getpass.getpass("Password: ")

    result = Client(host=host).login(email, password)
    token = result.get("token")
    if not token:
        raise VroxyError("Login succeeded but returned no token.")
    path = config.save(host, token, email=result.get("user", {}).get("email") or email)
    print(f"Signed in as {result.get('user', {}).get('email') or email} — token saved to {path}")


def cmd_logout(_args):
    print("Signed out." if config.clear() else "Nothing to sign out of.")


def cmd_whoami(args):
    payload = _client(args).me()
    user = payload.get("user", {})
    spaces = payload.get("workspaces", [])
    _emit(args, payload, lambda: print(
        f"{user.get('email', '?')}"
        f"{' (platform admin)' if user.get('admin') else ''}\n"
        f"{len(spaces)} workspace(s)"
    ))


def cmd_workspaces(args):
    rows = _client(args).workspaces()

    def plain():
        if not rows:
            print("No workspaces. A seat comes from accepting an invitation.")
            return
        for w in rows:
            waiting = w.get("awaiting_human_count") or 0
            flag = f"  {waiting} awaiting" if waiting else ""
            print(f"{w.get('hashid'):<12} {w.get('role', ''):<9} {w.get('name')}{flag}")

    _emit(args, rows, plain)


def cmd_rooms(args):
    rows = _client(args).rooms(args.workspace)

    def plain():
        for r in rows:
            unread = r.get("unread_count") or 0
            flag = f"  {unread} unread" if unread else ""
            print(f"{r.get('id'):<12} #{r.get('name')}{flag}")

    _emit(args, rows, plain)


def cmd_read(args):
    rows = _client(args).messages(args.workspace, args.room, limit=args.limit)

    def plain():
        for m in rows:
            who = m.get("sender_name") or m.get("author_name") or "?"
            print(f"[{m.get('created_at', '')[:16]}] {who}: {m.get('body', '')}")

    _emit(args, rows, plain)


def cmd_post(args):
    body = args.body if args.body != "-" else sys.stdin.read().strip()
    if not body:
        raise VroxyError("Nothing to post.")
    result = _client(args).post_message(args.workspace, args.room, body)
    _emit(args, result, lambda: print("Posted."))


def cmd_dispatch(args):
    payload = _client(args).dispatch_status(args.workspace)

    def plain():
        online = payload.get("online")
        print(f"{'running' if online else 'not running'} — {payload.get('summary', '')}")

    _emit(args, payload, plain)


def _body_from(args):
    if args.body == "-":
        return sys.stdin.read()
    if args.body:
        return args.body
    if getattr(args, "file", None):
        with open(args.file) as f:
            return f.read()
    return None


def cmd_docs(args):
    client = _client(args)
    action = args.action

    if action == "list":
        rows = client.docs(args.workspace, status=args.status, q=args.q)

        def plain():
            if not rows:
                print("No docs.")
                return
            for d in rows:
                print(f"{d.get('hashid'):<12} {d.get('status', ''):<10} {d.get('title')}")

        return _emit(args, rows, plain)

    if action == "show":
        doc = client.doc(args.workspace, args.doc)
        return _emit(args, doc, lambda: print(
            f"{doc.get('title')}  [{doc.get('status')}]\n\n{doc.get('body_md', '')}"
        ))

    if action == "create":
        body = _body_from(args)
        if not body:
            raise VroxyError("Give a body with --body, --file, or --body - for stdin.")
        doc = client.create_doc(args.workspace, title=args.title, body_md=body)
        return _emit(args, doc, lambda: print(f"Created {doc.get('hashid')} — {doc.get('title')}"))

    if action == "edit":
        body = _body_from(args)
        if body is None and args.title is None:
            raise VroxyError("Nothing to change. Pass --title and/or a body.")
        doc = client.update_doc(args.workspace, args.doc, title=args.title, body_md=body)
        return _emit(args, doc, lambda: print(f"Updated {doc.get('hashid')}."))

    if action in ("publish", "unpublish"):
        doc = client.publish_doc(args.workspace, args.doc, published=action == "publish")
        return _emit(args, doc, lambda: print(f"{doc.get('title')} is now {doc.get('status')}."))

    if action == "delete":
        result = client.delete_doc(args.workspace, args.doc)
        return _emit(args, result, lambda: print("Deleted."))


def cmd_members(args):
    client = _client(args)
    action = args.action

    if action == "list":
        payload = client.members(args.workspace)

        def plain():
            for m in payload.get("members", []):
                print(f"{m.get('membership_id'):<8} {m.get('role', ''):<9} {m.get('name')}")
            for inv in payload.get("invitations", []):
                print(f"{'pending':<8} {inv.get('role', ''):<9} {inv.get('email')}  ({inv.get('hashid')})")

        return _emit(args, payload, plain)

    if action == "invite":
        inv = client.invite_member(args.workspace, args.email, args.role)
        return _emit(args, inv, lambda: print(f"Invited {inv.get('email')} as {inv.get('role')}."))

    if action == "role":
        member = client.set_member_role(args.workspace, args.member, args.role)
        return _emit(args, member, lambda: print(f"{member.get('name')} is now {member.get('role')}."))

    if action == "remove":
        result = client.remove_member(args.workspace, args.member)
        return _emit(args, result, lambda: print(
            "You've left the workspace." if result.get("left") else "Removed."
        ))

    if action == "revoke":
        result = client.revoke_invitation(args.workspace, args.invitation)
        return _emit(args, result, lambda: print("Invitation revoked."))


def cmd_tools(args):
    client = _client(args)
    action = args.action

    if action == "list":
        rows = client.tools(args.workspace)

        def plain():
            if not rows:
                print("No custom tools.")
                return
            for t in rows:
                state = "on " if t.get("enabled") else "off"
                print(f"{t.get('hashid'):<12} {state} {t.get('kind', ''):<6} "
                      f"{t.get('access', ''):<7} {t.get('name')}")

        return _emit(args, rows, plain)

    if action == "show":
        tool = client.tool(args.workspace, args.tool)
        return _emit(args, tool, lambda: print(json.dumps(tool, indent=2)))

    if action == "create":
        tool = client.create_tool(
            args.workspace, name=args.name, label=args.label, description=args.description,
            kind=args.kind, access=args.access, url_template=args.url,
            follow_origin=args.follow_origin or None, params=_tool_params(args.param),
        )
        return _emit(args, tool, lambda: print(f"Created {tool.get('name')} ({tool.get('hashid')})."))

    if action in ("enable", "disable"):
        tool = client.toggle_tool(args.workspace, args.tool, enabled=action == "enable")
        return _emit(args, tool, lambda: print(
            f"{tool.get('name')} is {'enabled' if tool.get('enabled') else 'disabled'}."
        ))

    if action == "delete":
        result = client.delete_tool(args.workspace, args.tool)
        return _emit(args, result, lambda: print("Deleted."))


def _tool_params(raw):
    params = []
    for item in raw or []:
        name, _, description = item.partition("=")
        if not name.strip():
            raise VroxyError(f"--param needs name=description, got {item!r}")
        params.append({"name": name.strip(), "description": description.strip()})
    return params or None


def build_parser():
    p = argparse.ArgumentParser(prog="vroxy", description="Talk to a vroxy workspace.")
    p.add_argument("--version", action="version", version=f"vroxy {VERSION}")
    p.add_argument("--host", help="Defaults to $VROXY_HOST, then the saved host.")
    p.add_argument("--json", action="store_true", help="Raw JSON instead of a table.")
    sub = p.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Sign in and save a token")
    login.add_argument("--email")
    login.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="Forget the saved token").set_defaults(func=cmd_logout)
    sub.add_parser("whoami", help="Who the saved token belongs to").set_defaults(func=cmd_whoami)
    sub.add_parser("workspaces", help="Workspaces you hold a seat in").set_defaults(func=cmd_workspaces)

    rooms = sub.add_parser("rooms", help="Rooms in a workspace")
    rooms.add_argument("workspace")
    rooms.set_defaults(func=cmd_rooms)

    read = sub.add_parser("read", help="Recent messages in a room")
    read.add_argument("workspace")
    read.add_argument("room")
    read.add_argument("--limit", type=int, default=20)
    read.set_defaults(func=cmd_read)

    post = sub.add_parser("post", help="Post a message; body of - reads stdin")
    post.add_argument("workspace")
    post.add_argument("room")
    post.add_argument("body")
    post.set_defaults(func=cmd_post)

    disp = sub.add_parser("dispatch", help="Is the workspace's agent up")
    disp.add_argument("workspace")
    disp.set_defaults(func=cmd_dispatch)

    docs = sub.add_parser("docs", help="Knowledge-base docs the bot answers from")
    docs.add_argument("workspace")
    docs_sub = docs.add_subparsers(dest="action", required=True)
    docs_list = docs_sub.add_parser("list")
    docs_list.add_argument("--status", choices=["draft", "published"])
    docs_list.add_argument("--q", help="Loose search across title and body")
    for name in ("show", "publish", "unpublish", "delete"):
        docs_sub.add_parser(name).add_argument("doc")
    docs_create = docs_sub.add_parser("create")
    docs_create.add_argument("title")
    docs_create.add_argument("--body", help="Markdown body; - reads stdin")
    docs_create.add_argument("--file", help="Read the body from a file")
    docs_edit = docs_sub.add_parser("edit")
    docs_edit.add_argument("doc")
    docs_edit.add_argument("--title")
    docs_edit.add_argument("--body", help="Markdown body; - reads stdin")
    docs_edit.add_argument("--file")
    docs.set_defaults(func=cmd_docs, status=None, q=None, title=None, body=None, file=None)

    members = sub.add_parser("members", help="Who holds a seat, and at what level")
    members.add_argument("workspace")
    members_sub = members.add_subparsers(dest="action", required=True)
    members_sub.add_parser("list")
    invite = members_sub.add_parser("invite")
    invite.add_argument("email")
    invite.add_argument("role", choices=ROLES)
    role = members_sub.add_parser("role")
    role.add_argument("member", help="Membership id from `members list`")
    role.add_argument("role", choices=ROLES)
    members_sub.add_parser("remove").add_argument("member")
    members_sub.add_parser("revoke").add_argument("invitation", help="Invitation hashid")
    members.set_defaults(func=cmd_members)

    tools = sub.add_parser("tools", help="Custom bot tools")
    tools.add_argument("workspace")
    tools_sub = tools.add_subparsers(dest="action", required=True)
    tools_sub.add_parser("list")
    for name in ("show", "enable", "disable", "delete"):
        tools_sub.add_parser(name).add_argument("tool")
    make = tools_sub.add_parser("create")
    make.add_argument("name", help="lowercase letters, digits, underscores")
    make.add_argument("--label", required=True)
    make.add_argument("--description", required=True, help="What the model reads to decide when to call it")
    make.add_argument("--url", required=True, help="Template with {placeholders}")
    make.add_argument("--kind", choices=["link", "fetch"], default="link")
    make.add_argument("--access", choices=["public", "user", "admin"], default="public")
    make.add_argument("--param", action="append", metavar="NAME=DESCRIPTION")
    make.add_argument("--follow-origin", action="store_true", dest="follow_origin")
    tools.set_defaults(func=cmd_tools)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except VroxyError as e:
        print(f"vroxy: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
