import argparse
import getpass
import json
import os
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
