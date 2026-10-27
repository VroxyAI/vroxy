#!/usr/bin/env python3
"""Headless browser for dispatch agents and projects.

Any harness on the box can shell out to this to actually LOOK at a page
— screenshot it or dump its rendered text — instead of an operator
having to describe what is broken in words. Sign-in rides a single-use
magic link (the admin "issue a login link" flow) so the session is the
operator's choice, never a stored password.

Commands:
  vroxy-browser shot <url> [--out <png>]   full-page screenshot
  vroxy-browser text <url>                 rendered text of the page
  vroxy-browser login <magic-link>         click the one-time link, persist the session

The browser profile persists under $VROXY_BROWSER_PROFILE (default
~/.cache/vroxy-browser), so `login` once and later `shot`/`text` are
authenticated. Point the URL at the local dev stack (e.g.
http://localhost:3000) to inspect a project running locally.
"""
import argparse
import os
import sys
from pathlib import Path

PROFILE_DIR = Path(
    os.environ.get("VROXY_BROWSER_PROFILE", Path.home() / ".cache" / "vroxy-browser")
)


def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "playwright is not installed — run: pip install playwright && "
            "playwright install chromium"
        )
    return sync_playwright


def _ensure_profile():
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(PROFILE_DIR, 0o700)


def _new_page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def _goto(page, url):
    page.goto(url, wait_until="networkidle", timeout=60_000)


def shot(url, out):
    out = Path(out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    sync_playwright = _sync_playwright()
    _ensure_profile()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True)
        page = _new_page(ctx)
        _goto(page, url)
        page.screenshot(path=str(out), full_page=True)
        ctx.close()
    print(out)


def text(url):
    sync_playwright = _sync_playwright()
    _ensure_profile()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True)
        page = _new_page(ctx)
        _goto(page, url)
        print(page.inner_text("body"))
        ctx.close()


def login(link):
    sync_playwright = _sync_playwright()
    _ensure_profile()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True)
        page = _new_page(ctx)
        _goto(page, link)
        page.click('[data-role="token-login-submit"]', timeout=10_000)
        page.wait_for_load_state("networkidle")
        print(page.url)
        ctx.close()


def build_parser():
    p = argparse.ArgumentParser(
        prog="vroxy-browser", description="Headless browser for dispatch agents."
    )
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("shot", help="Full-page screenshot")
    s.add_argument("url")
    s.add_argument("--out", default="/tmp/vroxy-browser.png")
    t = sub.add_parser("text", help="Rendered text of the page")
    t.add_argument("url")
    l = sub.add_parser("login", help="Click a one-time magic login link, keep the session")
    l.add_argument("link")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.command == "shot":
        shot(args.url, args.out)
    elif args.command == "text":
        text(args.url)
    elif args.command == "login":
        login(args.link)
    return 0


if __name__ == "__main__":
    sys.exit(main())
