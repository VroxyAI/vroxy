#!/usr/bin/env python3
"""
vroxy_dispatch — an ActionCable client that subscribes to a
vroxy tenant's AdminFeedbackChannel and drives Claude Code
(headless) to answer admin UI-feedback notes.

Ported from vroxy_dispatch/feedback_agent.py with these vroxy-
specific adjustments:

  - **Tenant-scoped.** Vroxy's AdminFeedbackChannel is
    `stream_for(tenant)`, and connection auth is a Tenant-owned
    ApiToken with `platform:dispatch` or `full` scope.  So one
    dispatch process = one tenant.  (Vroxy is global.)

  - **Hashids everywhere.**  chat_id / message_id / feedback_id
    on the wire are hashids, not integer PKs.  The Rails side
    resolves them via `tenant.support_chats.find_by(hashid:)`.

  - **Cache-based heartbeat.**  Vroxy writes heartbeats into
    `Rails.cache` under `vroxy_dispatch:heartbeat:<tenant.id>`
    with a 60 s TTL — matches the vroxy design.  Payload shape:
    `{ "version": "...", "meta": {...} }`.

  - **Kind names.**  Approvals emit `approve.requested` back on
    THIS channel (dispatch subscribes to it).  Rejections emit
    `meta_update` on the chat's own SupportChatChannel — dispatch
    doesn't need to know because there's no code to apply.

Runtime shape mirrors vroxy_dispatch:

  1. Connect wss://vroxy.ai/cable?token=<TOKEN>.
  2. Subscribe { channel: "AdminFeedbackChannel" }.
  3. On feedback.created → build prompt → run claude-chat →
     parse an optional fenced ` ```proposal ``` ` block → reply.
  4. On approve.requested → write files, commit, push
     (inline_ship) or open a PR (pull_request).
  5. On room.message → answer in the workspace Room as the
     dispatch bot (conversation, not proposals — see
     handle_room_message).  Each room keeps its own Claude
     session; `/reset` in-room or `--reset` on the CLI clears it.
  6. Heartbeat every 20 s; one in-flight worker at a time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

# `websockets` is imported lazily inside `main()` so `import
# feedback_agent` (for unit tests around prompt building + parsing)
# works even without the dependency installed.
websockets = None  # populated in main()

# ── Config ────────────────────────────────────────────────────────
CABLE_URL       = os.environ.get("VROXY_CABLE_URL", "wss://vroxy.ai/cable")
SERVICE_TOKEN   = os.environ.get("VROXY_SERVICE_TOKEN", "")
# CODE_ROOT defaults to the directory that CONTAINS this dispatch
# checkout, so sibling repos (vroxy_web) resolve without env
# overrides regardless of whether we're deployed under
# `~/code/vroxy_dispatch` or `~/code/vroxy/vroxy_dispatch`.
CODE_ROOT       = Path(os.environ.get("CODE_ROOT",
                                       str(Path(__file__).resolve().parent.parent)))
PROJECT         = os.environ.get("PROJECT", "vroxy_web")
CLAUDE_CHAT_BIN = os.environ.get(
    "CLAUDE_CHAT_BIN",
    str(Path(__file__).resolve().parent / "bin" / "claude-chat"),
)
SID_DIR         = Path.home() / ".cache" / "claude-chat"

CHANNEL_IDENTIFIER = json.dumps({"channel": "AdminFeedbackChannel"})
AGENT_VERSION      = "vroxy_dispatch 0.2.0"
HEARTBEAT_INTERVAL_SECONDS = 20
# Must stay under the 4 s the room UI holds a typing state for
# (workspace_rooms.js `noteTyping`), or the indicator flickers.
ROOM_TYPING_INTERVAL_SECONDS = 3
# Rails caps a RoomMessage body at RoomMessage::BODY_MAX; the server
# truncates too, but splitting here keeps whole sentences.
ROOM_BODY_MAX = 4_000

# Mutable status reported on each heartbeat.  Updated by the worker
# as it moves through feedback / apply lifecycles so the admin UI
# can show "processing feedback abc123" in real time.
_current_status: str = "idle"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("vroxy_dispatch")


# ── ActionCable helpers ───────────────────────────────────────────
async def cable_send(ws, command: str, data: dict | None = None) -> None:
    """One ActionCable frame.  `command` is 'subscribe' or
    'message'; `data` is the per-command payload."""
    frame: dict[str, Any] = {"command": command, "identifier": CHANNEL_IDENTIFIER}
    if data is not None:
        frame["data"] = json.dumps(data)
    await ws.send(json.dumps(frame))


async def subscribe(ws) -> None:
    await cable_send(ws, "subscribe")


async def heartbeat(ws) -> None:
    """Heartbeat frame.  AdminFeedbackChannel#heartbeat writes it
    into Rails.cache under a tenant-scoped key with a 60 s TTL —
    the admin index card polls that key to show 🟢/🔴 + version."""
    await cable_send(ws, "message", {
        "action":  "heartbeat",
        "version": AGENT_VERSION,
        "meta":    {"status": _current_status, "project": PROJECT},
    })


async def heartbeat_forever(ws) -> None:
    """Background task: heartbeat every N seconds until the socket
    dies.  Errors are swallowed — the socket reader notices real
    disconnects and triggers reconnect at the outer level."""
    while True:
        try:
            await heartbeat(ws)
        except Exception:
            return
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def reply(ws, chat_id: str, body: str, kind: str = "assistant",
                proposal: dict | None = None) -> None:
    """Sends an `AdminFeedbackChannel#reply` action back over the
    socket — the server persists it as an assistant SupportChatMessage
    and re-broadcasts on SupportChatChannel.  Chat IDs are hashids."""
    payload: dict[str, Any] = {
        "action":  "reply",
        "chat_id": chat_id,
        "body":    body,
        "kind":    kind,
    }
    if proposal is not None:
        payload["proposal"] = proposal
    await cable_send(ws, "message", payload)
    log.info("Reply sent chat=%s kind=%s body=%.80s", chat_id, kind, body)


async def room_reply(ws, room_id: str, body: str, reply_to: str | None = None) -> None:
    """`AdminFeedbackChannel#room_reply` — the server posts it into
    the Room as the dispatch bot user through RoomMessageService, so
    it fans out to the room's cable, notifications, and webhooks
    exactly like a human message."""
    payload: dict[str, Any] = {
        "action":  "room_reply",
        "room_id": room_id,
        "body":    body,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    await cable_send(ws, "message", payload)
    log.info("Room reply sent room=%s body=%.80s", room_id, body)


async def room_typing(ws, room_id: str) -> None:
    """Ephemeral "dispatch is working" frame.  Nothing persists, so a
    run that dies just stops refreshing it."""
    await cable_send(ws, "message", {"action": "room_typing", "room_id": room_id})


async def room_typing_forever(ws, room_id: str) -> None:
    """Keeps the indicator alive for the length of a Claude run.  The
    interval is under the ~5 s the room UI holds a typing state for,
    so it never gaps."""
    while True:
        try:
            await room_typing(ws, room_id)
        except Exception:
            return
        await asyncio.sleep(ROOM_TYPING_INTERVAL_SECONDS)


# ── Prompt builder ────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are the UI-feedback agent for a Rails app.  A user filed a
feedback note (possibly with a picked element + screenshots).  Your
job:

1. Read the relevant files in this repo (start with the rendered
   partials from the feedback payload — those are the fastest way
   in).  For anything you're about to modify, READ THE FILE FIRST
   so you can reproduce its exact current content minus your edits.
2. Do the actual work.  Reply with a short (one-paragraph) plain-
   text explanation of the fix, then a fenced JSON proposal:

   ```proposal
   {
     "mode":    "inline_ship" | "pull_request",
     "summary": "one-line summary",
     "files":   [
       { "path": "app/views/foo/_bar.html.erb",
         "content": "<the COMPLETE new content of the file>" }
     ]
   }
   ```

3. Which mode?

   - **Default is `inline_ship`.**  The note is the fix request;
     ship it.  Size / line count / file count are NOT grounds for
     PR — a 400-line rewrite that solves the note is inline_ship.

   - Use **`pull_request`** ONLY when the note explicitly asks for
     one — look for words like "PR", "pull request", "branch",
     "in a branch", "for review", "don't ship yet", or a similar
     phrase.  PR mode is user-driven, not complexity-driven.

   - If the feedback is genuinely ambiguous or you can't do it
     without more info, reply with plain text asking the specific
     question.  NO fenced proposal block.

4. HARD RULES about `files`:

   - Every file's `content` MUST be the COMPLETE new file — the
     exact bytes we should `Write` to disk.  No ellipses, no
     "// ... unchanged ...", no TODO placeholders, no partial
     snippets, no "here's a sketch".  Preserve every unrelated
     section of the file verbatim (which is why you read it first).
   - If you can't produce complete content for a file, either drop
     it from the proposal or fall back to plain text (step 3
     ambiguous case).
   - Paths are relative to the project root (e.g.
     `app/views/foo/_bar.html.erb`, not absolute).
   - Absolutely no invented filenames or selectors — verify each
     path exists before including it.

Be concise; the user is watching this in a chat widget.
Investigate first, then commit real content.
""".strip()


ROOM_SYSTEM_PROMPT = """
You are Dispatch — Claude Code, headless, sitting in a workspace
chat room with the team that builds this repo.  You have full read
access to the checkout and the normal tools.

How to behave here:

- This is a CHAT, not a ticket queue.  Answer the question that was
  asked, at the length it deserves.  One line is a fine answer.
- You are talking to the people who wrote this code.  Skip the
  preamble, skip restating the question, skip the summary of what
  you're about to do.
- Investigate before answering anything factual about the code —
  read the files, run the greps.  Never guess at a filename, a
  version, or a behavior you haven't checked.
- Format for a chat window: short paragraphs, `code` spans for
  identifiers, fenced blocks only for real code.  No headings.
- If someone asks you to CHANGE something, you may edit files
  directly — you're in the working tree.  Say what you changed and
  in which files.  Do NOT commit or push unless asked explicitly.
- If a request is ambiguous, ask the one question that unblocks you
  rather than guessing.

The conversation so far is below.  Reply with only your message —
no "Dispatch:" prefix, no fenced proposal block.
""".strip()


def build_room_prompt(payload: dict) -> str:
    """Compose the prompt for an `AdminFeedbackChannel` `room.message`
    event.  The envelope carries the room, the triggering message, and
    up to ROOM_HISTORY_LIMIT prior turns (oldest first) so a Claude
    session that wasn't listening still knows what the room was
    talking about."""
    room    = payload.get("room") or {}
    msg     = payload.get("message") or {}
    sender  = payload.get("sender") or {}
    history = payload.get("history") or []

    lines: list[str] = [ROOM_SYSTEM_PROMPT, ""]

    name  = room.get("name") or "?"
    topic = (room.get("topic") or "").strip()
    lines.append(f"## Room: #{name}")
    if topic:
        lines.append(f"Topic: {topic}")
    lines.append(f"Repo: {PROJECT} (you are running inside its checkout)")
    lines.append("")

    if history:
        lines.append("## Recent conversation")
        for h in history:
            who  = h.get("sender") or "someone"
            body = _one_line(h.get("body") or "", 500)
            if body:
                lines.append(f"{who}: {body}")
        lines.append("")

    who  = sender.get("name") or "someone"
    body = (msg.get("body") or "").strip()
    lines.append("## The message to answer")
    lines.append(f"{who}: {body}")

    return "\n".join(lines)


def build_prompt(payload: dict) -> str:
    """Compose the user prompt for `claude-chat` from an
    `AdminFeedbackChannel` `feedback.created` event.  Vroxy's
    envelope has slightly flatter shape than vroxy's — feedback
    fields live under `payload["feedback"]` rather than at the top
    level, so accessors here match `broadcast_feedback_created`
    over on the Rails side."""
    fb        = payload.get("feedback") or {}
    note      = fb.get("note") or "(no note)"
    partials  = fb.get("rendered_partials") or []
    viewport  = {
        "width":  fb.get("viewport_width"),
        "height": fb.get("viewport_height"),
    }
    chat      = payload.get("chat") or {}
    visitor   = payload.get("visitor") or {}

    lines = [SYSTEM_PROMPT, "", "── Feedback ──", "", f"**Note:** {note}", ""]

    # Reporter block — comes from the vroxy gem's identify()
    # payload on the customer site (email + name + role) plus
    # whatever else the host app pushed under `meta`.  Role is
    # what tells Claude whether the reporter is a platform admin,
    # a regular signed-in user, or an anonymous visitor.
    role = visitor.get("role") or "anonymous"
    reporter_label = visitor.get("name") or visitor.get("email") or (
        "anonymous visitor" if role == "anonymous" else f"visitor {visitor.get('hashid', '?')}"
    )
    lines += [
        "**Reporter:**",
        f"- {reporter_label}",
        f"- Role: `{role}`",
    ]
    if visitor.get("email") and visitor.get("email") != reporter_label:
        lines.append(f"- Email: {visitor['email']}")
    if visitor.get("external_id"):
        lines.append(f"- External id: `{visitor['external_id']}`")
    # Surface the remaining meta keys (plan, tenant_id, custom tags,
    # etc.) so Claude can gate its response on them — but drop
    # `role` because we already printed it above.
    extras = {k: v for k, v in (visitor.get("meta") or {}).items()
              if k not in ("role",) and v not in (None, "", [], {})}
    if extras:
        lines.append(f"- Meta: {json.dumps(extras, sort_keys=True)}")
    lines.append("")

    lines += [
        "**Page context:**",
        f"- URL: {fb.get('page_url') or '(unknown)'}",
        f"- Path: {fb.get('page_path') or '(unknown)'}",
        f"- Controller#action: `{fb.get('controller_action') or '(unknown)'}`",
    ]
    if viewport.get("width") or viewport.get("height"):
        lines.append(f"- Viewport: {viewport.get('width') or '?'} × {viewport.get('height') or '?'}")
    lines.append("")

    if partials:
        lines += ["**Rendered partials (in order):**"]
        # `rendered_partials` on vroxy is JSONB of strings
        # (`"app/views/foo/_bar.html.erb|1.2ms"`) — normalize to the
        # path only for the prompt so Claude doesn't parse timings.
        seen: list[str] = []
        for p in partials[:40]:
            path = str(p).split("|", 1)[0].strip()
            if path and path not in seen:
                seen.append(path)
                lines.append(f"- `{path}`")
        if len(partials) > 40:
            lines.append(f"- … and {len(partials) - 40} more")
        lines.append("")

    if fb.get("selector"):
        lines += [
            "**Picked element:**",
            f"- Selector: `{fb['selector']}`",
            f"- Tag: `{fb.get('element_tag') or '?'}`",
        ]
        if fb.get("element_text"):
            lines.append(f'- Text: "{fb["element_text"]}"')
        if fb.get("element_html"):
            snippet = fb["element_html"][:400]
            lines += ["- outerHTML:", "  ```html", f"  {snippet}", "  ```"]
        lines.append("")
    else:
        lines += ["**No element picked** — this is a page-level note.", ""]

    if chat.get("hashid"):
        lines += [f"**Chat:** {chat.get('title') or '(untitled)'} ({chat['hashid']})", ""]

    lines += [
        "── Task ──",
        "",
        "Follow the response format from the system prompt.  Investigate",
        "the codebase first (rendered partials are the fastest way in),",
        "then reply.",
    ]

    return "\n".join(lines)


# ── Claude runner ─────────────────────────────────────────────────
PROPOSAL_FENCE_RE = re.compile(
    r"```proposal\s*(\{.*?\})\s*```",
    re.DOTALL,
)


def _streamed_sid_file(project: str) -> Path:
    """Session id for the STREAMED path — kept separate from
    claude-chat's file so single-shot and streamed conversations
    don't overwrite each other's ids."""
    return SID_DIR / f"feedback_stream_{project}"


def _room_session_key(room_hashid: str) -> str:
    """Each room gets its own Claude session so two rooms (and the
    feedback queue) never inherit each other's context."""
    return f"room_{room_hashid}_{PROJECT}"


def clear_sessions(keys: list[str] | None = None) -> list[str]:
    """Delete stored session ids so the next run starts a fresh
    Claude conversation.  Returns the names actually removed.

    With no keys, clears every session this project owns — both the
    streamed and the claude-chat file for PROJECT, plus every room.
    Used by `--reset` and by the in-room `/reset` command."""
    if not SID_DIR.is_dir():
        return []
    if keys:
        targets = [SID_DIR / f"feedback_stream_{k}" for k in keys]
    else:
        targets = [_streamed_sid_file(PROJECT), SID_DIR / f"feedback_{PROJECT}"]
        targets += [p for p in SID_DIR.glob(f"feedback_stream_room_*_{PROJECT}")]

    removed = []
    for path in targets:
        if path.exists():
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                log.exception("could not remove %s", path)
    return removed


# `/reset` and its aliases, matching bin/claude-chat's vocabulary.
RESET_COMMANDS = {"/reset", "/clear", "/new"}


def room_command(body: str) -> str | None:
    """A leading slash command in a room message, or None.  Only the
    first token counts — "/reset please" resets."""
    token = (body or "").strip().split(maxsplit=1)
    if not token:
        return None
    head = token[0].lower()
    return head if head in RESET_COMMANDS else None


def _resolve_claude_bin() -> str:
    """Same resolution order as bin/claude-chat: $CLAUDE_BIN → PATH
    → ~/.local/bin/claude → /usr/local/bin/claude.  Fail-loud so a
    'command not found' doesn't get swallowed as an empty response."""
    override = os.environ.get("CLAUDE_BIN")
    if override:
        return override
    from shutil import which
    on_path = which("claude")
    if on_path:
        return on_path
    for guess in (Path.home() / ".local/bin/claude", Path("/usr/local/bin/claude")):
        if guess.is_file() and os.access(guess, os.X_OK):
            return str(guess)
    raise FileNotFoundError("`claude` binary not found. Set CLAUDE_BIN or put it on PATH.")


def run_claude_streamed(prompt: str, project: str, on_event,
                        allow_resume: bool = True,
                        session_key: str | None = None) -> str:
    """`claude -p ... --output-format stream-json` variant.

    Emits each JSON event to `on_event(dict)` as it arrives so
    callers can forward tool_use / text events into a chat widget
    live.  Blocking; call from a thread.  Returns aggregated final
    result text.

    A stored session id can outlive the transcript it points at (a
    cleared history, a different host, `~/.claude` wiped).  `--resume`
    then exits 1 having produced nothing, so on that signature the
    session file is dropped and the prompt retried fresh once."""
    work_dir = str(CODE_ROOT / project)
    if not Path(work_dir).is_dir():
        raise FileNotFoundError(
            f"CODE_ROOT/{project} not found at {work_dir!r}. "
            f"Set CODE_ROOT and/or PROJECT env vars — CODE_ROOT currently = {CODE_ROOT!r}"
        )
    claude_bin = _resolve_claude_bin()
    sid_file = _streamed_sid_file(session_key or project)
    sid_file.parent.mkdir(parents=True, exist_ok=True)

    argv = [claude_bin, "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",  # required alongside stream-json in current CLIs
            "--dangerously-skip-permissions"]
    resumed = False
    if allow_resume and sid_file.exists() and sid_file.read_text().strip():
        argv += ["--resume", sid_file.read_text().strip()]
        resumed = True

    log.info("Running streamed claude in %s (session=%s, resume=%s)",
             work_dir, sid_file, resumed)
    proc = subprocess.Popen(
        argv, cwd=work_dir, env=os.environ.copy(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )

    final_text_chunks: list[str] = []
    new_sid: str | None = None

    # Compact tally so the log summary at end shows what Claude did.
    tool_calls = 0
    text_chars = 0
    thinking_chars = 0

    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = event.get("session_id") or event.get("sessionId")
            if sid:
                new_sid = sid

            etype = event.get("type")

            if etype == "assistant":
                content = ((event.get("message") or {}).get("content")) or event.get("content") or []
                for block in content:
                    btype = block.get("type")
                    if btype == "tool_use":
                        tool_calls += 1
                        name  = block.get("name") or ""
                        input = block.get("input") or {}
                        arg_preview = _compact_tool_args(input)
                        log.info("  → tool_use %s(%s)", name, arg_preview)
                        try:
                            on_event({"type": "tool_use", "name": name, "input": input})
                        except Exception:
                            log.exception("on_event tool_use raised")
                    elif btype == "text":
                        text = block.get("text") or ""
                        if text:
                            text_chars += len(text)
                            final_text_chunks.append(text)
                            log.info("  → text %s", _one_line(text, 200))
                            try:
                                on_event({"type": "text_delta", "text": text})
                            except Exception:
                                log.exception("on_event text raised")
                    elif btype == "thinking":
                        thought = block.get("thinking") or block.get("text") or ""
                        if thought:
                            thinking_chars += len(thought)
                            log.info("  · thinking %s", _one_line(thought, 200))

            elif etype == "user":
                content = ((event.get("message") or {}).get("content")) or event.get("content") or []
                for block in content:
                    if block.get("type") == "tool_result":
                        raw_r = block.get("content")
                        preview = _one_line(str(raw_r), 160) if raw_r is not None else "(no content)"
                        is_error = block.get("is_error")
                        marker = "⚠️ error" if is_error else "ok"
                        log.info("  ← tool_result %s %s", marker, preview)

            elif etype == "result":
                r = event.get("result")
                if isinstance(r, str) and r.strip():
                    final_text_chunks = [ r ]
                usage = event.get("usage") or {}
                if usage:
                    log.info("  · usage %s", usage)

        proc.wait(timeout=30)
    finally:
        if proc.stdout: proc.stdout.close()
        stderr_text = ""
        if proc.stderr:
            stderr_text = proc.stderr.read() or ""
            if stderr_text:
                log.info("claude stderr: %s", stderr_text[:400])
            proc.stderr.close()

    failed = proc.returncode not in (0, None)
    if failed:
        log.warning("streamed claude rc=%s", proc.returncode)

    produced_nothing = not final_text_chunks and tool_calls == 0

    if resumed and failed and produced_nothing:
        log.warning("resume produced nothing (rc=%s) — dropping stale session "
                    "id and retrying fresh", proc.returncode)
        try: sid_file.unlink(missing_ok=True)
        except Exception: log.exception("clearing session id failed")
        return run_claude_streamed(prompt, project, on_event,
                                   allow_resume=False, session_key=session_key)

    if new_sid and not failed:
        try: sid_file.write_text(new_sid)
        except Exception: log.exception("saving session id failed")

    log.info("streamed claude done — tool_calls=%d text_chars=%d thinking_chars=%d",
             tool_calls, text_chars, thinking_chars)

    if failed and produced_nothing:
        raise RuntimeError(
            f"claude exited {proc.returncode} with no output"
            + (f": {_one_line(stderr_text, 300)}" if stderr_text else ""))

    return "".join(final_text_chunks).strip()


def _one_line(text: str, limit: int) -> str:
    """Squash newlines + truncate — log-friendly preview."""
    if not text:
        return ""
    s = " ".join(text.split())
    return s if len(s) <= limit else s[:limit] + "…"


def _compact_tool_args(input_dict) -> str:
    """One-line summary of a tool_use's `input` for the log.  Big
    string values are truncated; nested structures are stringified
    compactly."""
    if not isinstance(input_dict, dict):
        return _one_line(str(input_dict), 120)
    parts = []
    for k, v in input_dict.items():
        if isinstance(v, str):
            parts.append(f"{k}={_one_line(v, 80)}")
        else:
            parts.append(f"{k}={_one_line(json.dumps(v, default=str), 80)}")
    return ", ".join(parts)[:200]


def run_claude(prompt: str, project: str) -> str:
    """Blocking subprocess to `claude-chat` — fallback path when
    the streamed run crashes (broken CLI flag on a Claude Code
    upgrade, etc.).  Called from a thread so the asyncio loop
    keeps ticking."""
    work_dir = str(CODE_ROOT / project)
    if not Path(work_dir).is_dir():
        raise FileNotFoundError(
            f"CODE_ROOT/{project} not found at {work_dir!r}. "
            f"Set CODE_ROOT and/or PROJECT env vars — CODE_ROOT currently = {CODE_ROOT!r}"
        )
    sid_file = str(SID_DIR / f"feedback_{project}")
    env = {
        **os.environ,
        "CLAUDE_SID_FILE":         sid_file,
        "CLAUDE_CHAT_EXTRA_ARGS":  "--dangerously-skip-permissions",
    }
    log.info("Running claude-chat in %s (session=%s)", work_dir, sid_file)
    result = subprocess.run(
        [CLAUDE_CHAT_BIN, prompt],
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        log.warning("claude-chat rc=%s stderr=%.500s", result.returncode, result.stderr)
    return result.stdout.strip()


def parse_proposal(raw: str) -> tuple[str, dict | None]:
    """Splits Claude's raw output into (chat_body, proposal_or_None).
    The body is what the visitor sees in the widget; the proposal
    (if any) is attached to the assistant message's meta and rendered
    as a code-proposal card with Apply/Ship-it/Reject."""
    match = PROPOSAL_FENCE_RE.search(raw)
    if not match:
        return raw, None
    json_blob = match.group(1)
    try:
        proposal = json.loads(json_blob)
    except json.JSONDecodeError as e:
        log.warning("Proposal JSON invalid: %s — treating as plain text", e)
        return raw, None
    body = PROPOSAL_FENCE_RE.sub("", raw).strip()
    return body, proposal


# ── Main event loop ───────────────────────────────────────────────
async def handle_approve(ws, payload: dict) -> None:
    """Operator clicked "Ship it" (or "Open PR") on a code-proposal
    card.  Vroxy's channel broadcasts `approve.requested` on the
    SAME AdminFeedbackChannel we're subscribed to, so dispatch
    picks it up here."""
    chat_id  = payload.get("chat_id")
    msg_id   = payload.get("message_id")
    # `approve.requested` doesn't carry the proposal itself — it
    # carries chat_id + message_id and expects us to re-derive the
    # proposal from the message we just persisted.  For MVP we
    # accept the proposal in the payload (populated by a future
    # server-side enrichment) OR skip; the widget handoff for the
    # bare id case comes in a follow-up.
    proposal = payload.get("proposal") or {}
    files    = proposal.get("files") or []
    mode     = proposal.get("mode") or "inline_ship"
    summary  = proposal.get("summary") or "code proposal"
    if not chat_id:
        return
    if not files:
        # No inline proposal to apply — log and no-op so the admin
        # UI's approve click at least gets a friendly reply.
        await reply(ws, chat_id, "⚠️ Approve received but no proposal payload — nothing to apply.")
        return

    project_dir = CODE_ROOT / PROJECT
    if not project_dir.is_dir():
        await reply(ws, chat_id, f"⚠️ Can't apply: {project_dir} not found.")
        return

    try:
        # Write every file to disk (paths are RELATIVE to the project).
        for f in files:
            path = f.get("path")
            content = f.get("content")
            if not path or content is None:
                continue
            target = project_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        if mode == "pull_request":
            outcome = await asyncio.to_thread(_git_open_pr, project_dir, summary, files)
        else:
            outcome = await asyncio.to_thread(_git_inline_ship, project_dir, summary, files)

        await reply(ws, chat_id, outcome)
    except Exception as e:
        log.exception("apply failed")
        await reply(ws, chat_id, f"⚠️ Apply failed: {type(e).__name__}: {e}")


def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    """Small subprocess helper — returncode, stdout, stderr."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _git_inline_ship(project_dir: Path, summary: str, files: list[dict]) -> str:
    """Small-tweak path: commit the changed files, run
    version_bump.sh if present (vroxy_web has one), push straight
    to origin/HEAD which auto-deploys."""
    paths = [ f["path"] for f in files if f.get("path") ]
    log.info("Inline ship: %s → %s", summary, paths)

    _run(["git", "add"] + paths, project_dir)

    vb = project_dir / "version_bump.sh"
    if vb.is_file():
        rc, _, err = _run(["bash", "./version_bump.sh"], project_dir)
        if rc == 0:
            _run(["git", "add", "config/application.rb", "CHANGELOG.md"], project_dir)
        else:
            log.warning("version_bump.sh failed: %s", err[:200])

    msg = f"UI-feedback tweak: {summary}"
    rc, _, err = _run(["git", "commit", "-m", msg], project_dir)
    if rc != 0:
        return f"⚠️ Commit failed: {err[:300]}"

    rc, out, err = _run(["git", "rev-parse", "HEAD"], project_dir)
    sha = (out or "").strip()[:12]

    rc, _, err = _run(["git", "push", "origin", "HEAD"], project_dir)
    if rc != 0:
        return f"⚠️ Push failed after commit {sha}: {err[:300]}"

    return f"✅ Shipped `{sha}` — {summary}. Auto-deploy is rolling."


def _git_open_pr(project_dir: Path, summary: str, files: list[dict]) -> str:
    """Bigger-change path: branch + commit + push + `gh pr create`."""
    paths = [ f["path"] for f in files if f.get("path") ]
    branch = "feedback/" + re.sub(r"[^a-z0-9-]+", "-", summary.lower())[:40].strip("-")
    if not branch or branch == "feedback/":
        branch = f"feedback/ui-{int(__import__('time').time())}"

    log.info("Open PR: %s on branch %s", summary, branch)

    _run(["git", "checkout", "-b", branch], project_dir)
    _run(["git", "add"] + paths, project_dir)
    rc, _, err = _run(["git", "commit", "-m", f"UI feedback: {summary}"], project_dir)
    if rc != 0:
        return f"⚠️ Commit failed: {err[:300]}"

    rc, _, err = _run(["git", "push", "-u", "origin", branch], project_dir)
    if rc != 0:
        return f"⚠️ Push failed: {err[:300]}"

    rc, out, err = _run(
        ["gh", "pr", "create", "--title", f"UI feedback: {summary}",
         "--body", "Filed via vroxy_dispatch from an admin UI-feedback note."],
        project_dir,
    )
    if rc != 0:
        return f"⚠️ `gh pr create` failed: {err[:300]}"
    return f"✅ PR opened on `{branch}` — {out.strip()}"


async def _emit_progress(ws, chat_id: str, name: str, input_dict: dict | None) -> None:
    """One `progress` action on AdminFeedbackChannel — Rails
    persists a `tool_call` SupportChatMessage row + broadcasts a
    live chip on SupportChatChannel so the widget shows Claude
    working in real time.  Non-fatal on error."""
    try:
        compact = {}
        if isinstance(input_dict, dict):
            for k, v in input_dict.items():
                if isinstance(v, str) and len(v) > 200:
                    compact[k] = v[:200] + "…"
                else:
                    compact[k] = v
        await cable_send(ws, "message", {
            "action":  "progress",
            "chat_id": chat_id,
            "name":    name,
            "input":   compact,
        })
    except Exception:
        log.exception("emit_progress failed")


def _log_future_error(fut) -> None:
    """add_done_callback target for the scheduled progress coroutines
    so we notice silent failures.  Exceptions inside
    run_coroutine_threadsafe'd coroutines get stashed on the Future
    and never surfaced by default — that's the class of bug that
    made progress silently no-op end-to-end during vroxy's first
    bring-up."""
    try:
        exc = fut.exception()
    except Exception:
        return
    if exc is not None:
        log.warning("emit_progress future raised: %r", exc)


# When True, dispatch runs Claude in streaming mode and forwards
# each tool_use over the socket as a live chip.  Falls back to the
# non-streamed subprocess path on error so a broken Claude CLI
# flag doesn't take feedback processing offline.
STREAM_ENABLED = os.environ.get("CLAUDE_STREAM", "1") != "0"


async def handle_feedback(ws, payload: dict) -> None:
    """`feedback.created` handler.  Vroxy's envelope has:
        payload["feedback"] — the AdminUiFeedback JSON
        payload["chat"]     — { id, hashid, title }
        payload["message"]  — { id, hashid, body }  (the original note)
    We reply on the chat via `reply` → assistant message + Support-
    ChatChannel `done` broadcast."""
    chat = payload.get("chat") or {}
    fb   = payload.get("feedback") or {}
    chat_id = chat.get("hashid")
    if not chat_id:
        log.warning("feedback.created without chat.hashid: %s", payload)
        return

    prompt = build_prompt(payload)
    log.info("Handling feedback id=%s chat=%s note=%.80s",
             fb.get("hashid"), chat_id, fb.get("note"))

    # Kick off with an "investigating" chip so the visitor sees
    # dispatch is alive during the first pre-tool seconds.
    await _emit_progress(ws, chat_id, "investigating…",
                         {"feedback_id": fb.get("hashid")})

    loop = asyncio.get_running_loop()

    def on_stream_event(event: dict) -> None:
        etype = event.get("type")
        if etype == "tool_use":
            name = event.get("name") or "tool"
            fut = asyncio.run_coroutine_threadsafe(
                _emit_progress(ws, chat_id, name, event.get("input")), loop)
            fut.add_done_callback(_log_future_error)

    try:
        if STREAM_ENABLED:
            try:
                raw = await asyncio.to_thread(
                    run_claude_streamed, prompt, PROJECT, on_stream_event)
            except FileNotFoundError:
                raise
            except Exception:
                log.exception("streamed run failed — falling back to non-streamed")
                raw = await asyncio.to_thread(run_claude, prompt, PROJECT)
        else:
            raw = await asyncio.to_thread(run_claude, prompt, PROJECT)
    except subprocess.TimeoutExpired:
        await reply(ws, chat_id, "⚠️ Claude timed out after 30 minutes.")
        return
    except Exception as e:
        log.exception("claude crashed")
        await reply(ws, chat_id, f"⚠️ Claude crashed: {type(e).__name__}: {e}")
        return

    if not raw:
        await reply(ws, chat_id, "(Claude returned an empty response.)")
        return

    body, proposal = parse_proposal(raw)
    if proposal:
        await reply(ws, chat_id, body or proposal.get("summary", ""),
                    kind="code_proposal", proposal=proposal)
    else:
        await reply(ws, chat_id, body)


def split_room_body(text: str, limit: int = ROOM_BODY_MAX) -> list[str]:
    """Claude can outrun a room message's length cap.  Split on
    paragraph, then line, then hard characters — a fenced block that
    exceeds the cap still has to break somewhere."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


async def handle_room_message(ws, payload: dict) -> None:
    """`room.message` handler — dispatch's turn in a workspace room.

    Unlike feedback, a room answer is plain conversation: no proposal
    block, no tool chips (a room log full of tool calls is noise), and
    a per-room Claude session so two rooms stay independent."""
    room    = payload.get("room") or {}
    msg     = payload.get("message") or {}
    sender  = payload.get("sender") or {}
    room_id = room.get("hashid")
    if not room_id:
        log.warning("room.message without room.hashid: %s", payload)
        return

    body = (msg.get("body") or "").strip()
    log.info("Handling room message room=%s (#%s) from=%s body=%.80s",
             room_id, room.get("name"), sender.get("name"), body)

    command = room_command(body)
    if command:
        removed = clear_sessions([_room_session_key(room_id)])
        await room_reply(ws, room_id,
                         "🧹 Fresh session — I've forgotten this room's history."
                         if removed else
                         "🧹 Already on a fresh session (nothing to clear).")
        return

    prompt = build_room_prompt(payload)
    typing_task = asyncio.create_task(room_typing_forever(ws, room_id))
    try:
        raw = await asyncio.to_thread(
            run_claude_streamed, prompt, PROJECT, lambda e: None,
            True, _room_session_key(room_id))
    except subprocess.TimeoutExpired:
        await room_reply(ws, room_id, "⚠️ I timed out after 30 minutes.")
        return
    except Exception as e:
        log.exception("claude crashed on room message")
        await room_reply(ws, room_id, f"⚠️ I crashed: {type(e).__name__}: {e}")
        return
    finally:
        typing_task.cancel()

    if not raw:
        await room_reply(ws, room_id, "(I came back with an empty response.)")
        return

    for chunk in split_room_body(raw):
        await room_reply(ws, room_id, chunk)


async def process_stream(ws) -> None:
    """One in-flight handler at a time — Claude sessions aren't
    reentrant and we don't want to race a `--resume` with itself."""
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    async def worker():
        global _current_status
        while True:
            kind, payload = await queue.get()
            fb_id = ((payload.get("feedback") or {}).get("hashid")
                     or payload.get("feedback_id") or "?")
            if kind == "room":
                room = payload.get("room") or {}
                _current_status = f"answering #{room.get('name') or room.get('hashid')}"
            else:
                _current_status = f"processing feedback {fb_id}" if kind == "feedback" \
                                  else f"applying feedback {fb_id}"
            try:
                if kind == "feedback":
                    await handle_feedback(ws, payload)
                elif kind == "approve":
                    await handle_approve(ws, payload)
                elif kind == "room":
                    await handle_room_message(ws, payload)
            except Exception:
                log.exception("worker failed on payload=%s", payload)
            finally:
                _current_status = "idle"
                queue.task_done()

    worker_task    = asyncio.create_task(worker())
    heartbeat_task = asyncio.create_task(heartbeat_forever(ws))

    try:
        async for raw in ws:
            frame = json.loads(raw)
            frame_type = frame.get("type")

            if frame_type == "welcome":
                log.info("Cable connected — subscribing")
                await subscribe(ws)
                continue
            if frame_type == "confirm_subscription":
                log.info("Subscribed to AdminFeedbackChannel")
                continue
            if frame_type in ("ping", "disconnect", "reject_subscription"):
                if frame_type == "reject_subscription":
                    log.error("Subscription rejected — is VROXY_SERVICE_TOKEN a Tenant-owned "
                              "ApiToken with platform:dispatch or full scope?")
                continue

            msg = frame.get("message")
            if not msg:
                continue
            # Vroxy channel keys on `type` in the message payload,
            # matching vroxy's convention.
            if msg.get("type") == "feedback.created":
                await queue.put(("feedback", msg))
            elif msg.get("type") == "feedback.followup":
                # Followup carries just chat/message/feedback ids —
                # re-enter handle_feedback with the fresh chat state
                # so Claude picks up the new turn.
                await queue.put(("feedback", msg))
            elif msg.get("type") == "approve.requested":
                await queue.put(("approve", msg))
            elif msg.get("type") == "room.message":
                await queue.put(("room", msg))
            else:
                log.debug("Ignoring message type=%s", msg.get("type"))
    finally:
        worker_task.cancel()
        heartbeat_task.cancel()


async def main() -> None:
    global websockets
    import websockets as _ws  # deferred so unit tests don't need it installed
    websockets = _ws

    if not SERVICE_TOKEN:
        raise SystemExit("VROXY_SERVICE_TOKEN is required")
    uri = f"{CABLE_URL}?token={SERVICE_TOKEN}"
    # Rails' ActionCable enforces `allowed_request_origins` on prod;
    # a WS handshake without an `Origin` header comes back as 404.
    # We synthesize one from the cable URL's scheme + host so this
    # works against local dev + wss://vroxy.ai without a config knob.
    parsed = urllib.parse.urlparse(CABLE_URL)
    origin_scheme = "https" if parsed.scheme == "wss" else "http"
    origin = f"{origin_scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    log.info("Connecting to %s (origin=%s)", CABLE_URL, origin)

    backoff = 1
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30, origin=origin) as ws:
                backoff = 1
                await process_stream(ws)
        except (websockets.ConnectionClosed, OSError) as e:
            log.warning("Cable connection lost (%s) — reconnecting in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception:
            log.exception("Unexpected error in cable loop")
            await asyncio.sleep(5)


def cli() -> None:
    """`--reset` clears stored Claude session ids and exits; anything
    else starts the cable agent.  Reset is the escape hatch for a
    poisoned or wandering session — dispatch also self-heals a session
    id whose transcript has vanished, but a session that is merely
    WRONG (too long, off on a tangent) needs a human to say so."""
    if "--reset" in sys.argv:
        keys = [a for a in sys.argv[1:] if not a.startswith("-")]
        removed = clear_sessions([_room_session_key(k) for k in keys] if keys else None)
        scope = f"room(s) {', '.join(keys)}" if keys else f"project {PROJECT}"
        if removed:
            print(f"cleared {len(removed)} session(s) for {scope}:")
            for name in removed:
                print(f"  {name}")
        else:
            print(f"no stored sessions for {scope} — nothing to clear")
        return

    if "--sessions" in sys.argv:
        files = sorted(SID_DIR.glob("*")) if SID_DIR.is_dir() else []
        if not files:
            print(f"no stored sessions in {SID_DIR}")
            return
        for path in files:
            print(f"{path.name}\t{path.read_text().strip()}")
        return

    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__ or "")
        print("usage: feedback_agent.py [--reset [ROOM_HASHID …]] [--sessions] [--help]")
        print()
        print("  (no args)   run the cable agent")
        print("  --reset     clear this project's stored Claude sessions "
              "(feedback + every room); pass room hashids to clear only those")
        print("  --sessions  list stored session ids")
        return

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
