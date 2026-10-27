---
name: browser
description: See a web page with a headless browser — screenshot it or read its rendered text — instead of asking the operator to describe what's broken. Use when a message says something "looks broken", a page renders wrong, or you need to actually see a UI. Sign-in rides a single-use magic link the operator issues. Applies to every harness.
user-invocable: true
---

Look at a page with the box's headless browser
(`~/.local/bin/vroxy-browser`, Playwright + Chromium, no window).

## Commands

```bash
vroxy-browser shot <url> [--out /tmp/x.png]   # full-page screenshot
vroxy-browser text <url>                       # rendered text of the page
vroxy-browser login <magic-link>               # click the one-time link, keep the session
```

The session persists under `~/.cache/vroxy-browser` (0700), so `login`
once and later `shot`/`text` are authenticated. Point the URL at the
local dev stack to inspect a project running locally — the vroxy web
app is at `http://localhost:3002`, and each project's compose stack has
its own mapped port (`docker compose ps` shows it).

## When to use

- Someone says something is broken or "looks wrong" — screenshot it and
  read the DOM text instead of asking them to describe it.
- You shipped a UI change and want to confirm what actually rendered.
- You need to read a page that requires sign-in.

## Signing in (magic link)

You cannot mint a login link yourself — impersonation is a human's
choice. The operator issues one from the admin user page
(`/admin/mgmt/users/:id` → "Issue a login link", single-use, short
TTL) and pastes the link into the room. Then:

```bash
vroxy-browser login "<the full magic-link URL>"
vroxy-browser shot "https://vroxy.ai/w/<hashid>/<slug>/…"
```

The confirm page's "Sign in" is clicked for you; the cookie persists
in the profile so the next `shot`/`text` is signed in.

## Hard no

- Do not mint login links yourself, and do not store one in a file or
  past a screenshot of it anywhere. A link is the person's password.
- Do not use the browser session to act *as* the person (post, change,
  approve). It is for LOOKING — read screenshots and text, then make
  the change through the normal write paths.
- Do not leave the profile's cookies readable by others; the tool
  already chmods `~/.cache/vroxy-browser` to 0700, keep it that way.
