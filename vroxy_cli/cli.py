import argparse
import getpass
import json
import os
import socket
import sys

from . import config
from .client import Client, VroxyError
from .version import VERSION


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

    previous = config.load()
    result = Client(host=host).login(email, password, device=socket.gethostname() or None)
    token = result.get("token")
    if not token:
        raise VroxyError("Login succeeded but returned no token.")
    path = config.save(host, token, email=result.get("user", {}).get("email") or email)
    print(f"Signed in as {result.get('user', {}).get('email') or email} — token saved to {path}")

    if previous.get("token") and previous["token"] != token:
        revoked, reason = _revoke(previous.get("host") or host, previous["token"])
        if not revoked:
            print(f"Couldn't revoke the previous token ({reason}). "
                  f"Revoke it under Settings → Agent / CLI.", file=sys.stderr)


def cmd_logout(_args):
    saved = config.load()
    if not saved.get("token"):
        print("Nothing to sign out of.")
        return

    revoked, reason = _revoke(saved.get("host"), saved["token"])
    config.clear()
    if revoked:
        print("Signed out — the token is revoked.")
    else:
        print(f"Signed out locally, but the token could not be revoked ({reason}). "
              f"Revoke it under Settings → Agent / CLI.", file=sys.stderr)


def _revoke(host, token):
    try:
        Client(host=host, token=token).logout()
    except VroxyError as e:
        if e.status == 401:
            return True, None
        return False, str(e)
    return True, None


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


def cmd_search(args):
    """Room-message search across joined rooms in a workspace."""
    client = _client(args)
    rows = client.search_rooms(args.workspace, args.query, limit=args.limit)

    def plain():
        if not rows:
            print("No matches.")
            return
        for r in rows:
            when = (r.get("created_at") or "")[:16].replace("T", " ")
            room = r.get("room_name") or r.get("room_id") or "?"
            who = r.get("sender_name") or "?"
            body = (r.get("body") or "").replace("\n", " ")
            if len(body) > 120:
                body = body[:117] + "…"
            print(f"#{room}  {when}  {who}: {body}")

    return _emit(args, rows, plain)


def cmd_chats(args):
    client = _client(args)
    action = args.action

    if action == "list":
        payload = client.chats(
            args.workspace, filter=args.filter, q=args.q, page=args.page
        )
        rows = payload.get("chats", [])

        def plain():
            if not rows:
                print("No chats.")
                return
            for c in rows:
                title = (c.get("title") or "(untitled)")[:60]
                preview = (c.get("preview") or "").replace("\n", " ")
                if len(preview) > 80:
                    preview = preview[:77] + "…"
                print(f"{c.get('id'):<12} {title}")
                if preview:
                    print(f"             {preview}")

        return _emit(args, payload, plain)

    if action == "show":
        payload = client.chat(args.workspace, args.chat)
        return _emit(args, payload, lambda: print(json.dumps(payload, indent=2)))


def _one_line(text, width):
    text = (text or "").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def cmd_errors(args):
    client = _client(args)

    if args.action == "list":
        payload = client.errors(args.workspace, source=args.source, page=args.page)
        rows = payload.get("errors", [])

        def plain():
            if not rows:
                print("No errors.")
                return
            for e in rows:
                seen = (e.get("last_seen_at") or "")[:16].replace("T", " ")
                print(f"{e.get('fingerprint'):<18} {e.get('occurrences', 0):>5}x  {seen}  "
                      f"{e.get('error_class') or '?'}: {_one_line(e.get('message'), 70)}")
            _page_footer(payload)

        return _emit(args, payload, plain)

    if args.action == "show":
        payload = client.error(args.workspace, args.fingerprint, page=args.page)

        def plain():
            e = payload.get("error", {})
            print(f"{e.get('error_class')}: {e.get('message') or ''}")
            print(f"{e.get('occurrences', 0)} occurrence(s), last {e.get('last_seen_at')}, "
                  f"source {e.get('source')}")
            for frame in (e.get("backtrace") or [])[:15]:
                print(f"  {frame}")
            for o in payload.get("occurrences", []):
                print(f"- {o.get('occurred_at')}  {o.get('url') or o.get('request_path') or ''}")

        return _emit(args, payload, plain)


def cmd_usage(args):
    payload = _client(args).usage(args.workspace)

    def plain():
        plan = payload.get("plan", {})
        print(f"{plan.get('name')} ({plan.get('price_label')})"
              f"{'' if payload.get('enforced') else '  — limits not enforced yet'}")
        for m in payload.get("metrics", []):
            limit = "unlimited" if m.get("limit") is None else m.get("limit")
            window = f" ({m['window']})" if m.get("window") else ""
            print(f"  {m.get('label')}{window}: {m.get('used')} / {limit}")
        included = [f["feature"] for f in payload.get("features", []) if f.get("included")]
        if included:
            print(f"  Includes: {', '.join(included)}")

    _emit(args, payload, plain)


def cmd_visitors(args):
    client = _client(args)

    if args.action == "list":
        payload = client.visitors(
            args.workspace, identity=args.identity, active=args.active, page=args.page
        )
        rows = payload.get("visitors", [])

        def plain():
            if not rows:
                print("No visitors.")
                return
            for v in rows:
                seen = (v.get("last_seen_at") or "")[:16].replace("T", " ")
                gone = "  (deleted)" if v.get("deleted") else ""
                print(f"{v.get('id'):<12} {seen:<16}  {v.get('display')}{gone}")
            _page_footer(payload)

        return _emit(args, payload, plain)

    if args.action == "show":
        payload = client.visitor(args.workspace, args.visitor)
        return _emit(args, payload, lambda: print(json.dumps(payload, indent=2)))


def cmd_targets(args):
    payload = _client(args).targets(args.workspace, page=args.page)
    rows = payload.get("targets", [])

    def plain():
        if not rows:
            print("No dispatch targets.")
            return
        for t in rows:
            effective = t.get("effective") or {}
            repo = (t.get("repo") or {}).get("full_name") or "-"
            agent = (t.get("agent") or {}).get("name") or "-"
            state = "online" if t.get("online") else "offline"
            print(f"{t.get('name'):<20} {repo:<28} {effective.get('base_ref') or '-':<12} "
                  f"{effective.get('policy') or '-':<12} {agent}  [{state}]")

    _emit(args, payload, plain)


def _page_footer(payload):
    meta = payload.get("meta") or {}
    if (meta.get("total_pages") or 1) > 1:
        print(f"page {meta.get('current_page')} of {meta.get('total_pages')} "
              f"({meta.get('total_count')} total) — --page N for more")


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

    sub.add_parser("logout", help="Revoke the saved token and forget it").set_defaults(func=cmd_logout)
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

    search = sub.add_parser(
        "search", help="Search messages across rooms you have joined"
    )
    search.add_argument("workspace")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(func=cmd_search)

    chats = sub.add_parser("chats", help="Visitor support chats in a workspace")
    chats.add_argument("workspace")
    chats_sub = chats.add_subparsers(dest="action", required=True)
    chats_list = chats_sub.add_parser("list")
    chats_list.add_argument(
        "--filter", choices=["open", "awaiting_human", "all"], default="open"
    )
    chats_list.add_argument("--q", help="Loose search across title, visitor, messages")
    chats_list.add_argument("--page", type=int)
    chats_sub.add_parser("show").add_argument("chat")
    chats.set_defaults(func=cmd_chats, filter="open", q=None, page=None)

    errors = sub.add_parser("errors", help="Client errors reported into a workspace")
    errors.add_argument("workspace")
    errors_sub = errors.add_subparsers(dest="action", required=True)
    errors_list = errors_sub.add_parser("list")
    errors_list.add_argument("--source", help="ruby, js, api, mobile, node, python, php")
    errors_list.add_argument("--page", type=int)
    errors_show = errors_sub.add_parser("show")
    errors_show.add_argument("fingerprint", help="Fingerprint from `errors list`")
    errors_show.add_argument("--page", type=int, help="Page of occurrences")
    errors.set_defaults(func=cmd_errors, source=None, page=None)

    usage = sub.add_parser("usage", help="Plan, usage this month, and what the plan includes")
    usage.add_argument("workspace")
    usage.set_defaults(func=cmd_usage)

    visitors = sub.add_parser("visitors", help="People who have opened the widget")
    visitors.add_argument("workspace")
    visitors_sub = visitors.add_subparsers(dest="action", required=True)
    visitors_list = visitors_sub.add_parser("list")
    visitors_list.add_argument("--identity", choices=["identified", "anonymous"])
    visitors_list.add_argument("--active", action="store_true", help="Seen in the last 24 hours")
    visitors_list.add_argument("--page", type=int)
    visitors_sub.add_parser("show").add_argument("visitor")
    visitors.set_defaults(func=cmd_visitors, identity=None, active=False, page=None)

    targets = sub.add_parser("targets", help="What each dispatch agent works on, and how it ships")
    targets.add_argument("workspace")
    targets.add_argument("--page", type=int)
    targets.set_defaults(func=cmd_targets)

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
