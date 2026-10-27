---
name: browser
description: Use a real browser — the `vroxy-browser` MCP tools (navigate, click, type, fill forms, read the page, console, screenshots) or the `vroxy-browser` CLI for a one-shot screenshot/text — instead of asking the operator to describe what's broken. Use when a page "looks broken", a UI change needs checking, a flow needs clicking through, or you need to actually see a UI. Sign-in rides a single-use magic link the operator issues. Applies to every harness.
user-invocable: true
---

Two ways to use the box's headless Chromium (Playwright, no window).

## MCP tools — drive the page (preferred)

Every harness on the box has the **`vroxy-browser`** MCP server
registered (`bin/install-browser-mcp` in vroxy_dispatch did it). Its
tools appear as `browser_*` — in some harnesses prefixed with the
server name (`vroxy-browser_browser_click`, `mcp__vroxy-browser__…`):

- `browser_navigate` / `browser_navigate_back` / `browser_tabs`
- `browser_snapshot` — the page as an accessibility tree with `ref`s;
  read it before acting, then pass a ref as `target` to
- `browser_click`, `browser_type`, `browser_fill_form`,
  `browser_select_option`, `browser_press_key`, `browser_hover`
- `browser_console_messages`, `browser_network_requests` — what broke
- `browser_take_screenshot`, `browser_wait_for`, `browser_evaluate`

Each harness run gets its own in-memory profile: concurrent agents never
share cookies, and nothing persists after the run. A run starts signed
OUT; to sign in, navigate to a magic link the operator pasted (below).
Snapshots and screenshots land in `~/.cache/vroxy-browser-mcp`.

## CLI — one-shot look

`~/.local/bin/vroxy-browser`, for a quick screenshot or text dump from
a shell:

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
- Clicking through a UI to test it is the point — local dev stacks,
  your own test accounts, public pages. But do not use a session signed
  in with someone's magic link to act *as* them (post, change, approve,
  pay). With their session, LOOK; make the change through the normal
  write paths.
- Do not leave the profile's cookies readable by others; the tool
  already chmods `~/.cache/vroxy-browser` to 0700, keep it that way.
