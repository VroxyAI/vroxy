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
  3. On feedback.created → build prompt → run Claude in a
     THROWAWAY GIT WORKTREE (see proposal_worktree) → parse an
     optional fenced ` ```proposal ``` ` block → reply with the
     proposal and its exact diffstat.  The real checkout is never
     touched before approval.
  4. On approve.requested → write files, then commit to the base
     branch or open a PR, as the SERVER's ship policy directs
     (payload["policy"]["mode"]).  Dispatch obeys; it does not
     decide.
  5. On room.message → answer in the workspace Room as the
     dispatch bot (conversation, not proposals — see
     handle_room_message).  Each room keeps its own Claude
     session; `/reset` in-room or `--reset` on the CLI clears it.
  6. Heartbeat every 20 s; one in-flight worker at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import contextvars
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from logging.handlers import RotatingFileHandler
from queue import Empty, SimpleQueue
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
# Which CLI actually does the work.  `claude` or `codex`; anything
# else is refused at startup rather than silently falling back, so a
# typo in the unit file can't quietly run the wrong model.
DISPATCH_ENGINE = os.environ.get("DISPATCH_ENGINE", "claude").strip().lower()

# Self-update: dispatch edits its own checkout often enough that a
# run can leave the process running code that no longer exists on
# disk.  The unit name is what a restart targets; the state dir is
# how a notice survives the process that wrote it.
# Who this process is. `INSTALL_ID` is written once by install.sh and
# is what lets SEVERAL dispatch processes serve one workspace: the
# server resolves the agent row by it instead of the old "exactly one
# local agent or we can't tell who called" rule. `AGENT_NAME` is only
# a preference — the server makes it unique per workspace.
INSTALL_ID      = os.environ.get("VROXY_INSTALL_ID", "")
AGENT_NAME      = os.environ.get("VROXY_AGENT_NAME", "")

SERVICE_UNIT    = os.environ.get("VROXY_DISPATCH_UNIT",
                                 "vroxy-dispatch-feedback-agent.service")
RESTART_DELAY_SECONDS = int(os.environ.get("VROXY_DISPATCH_RESTART_DELAY", "5"))
STATE_DIR       = Path(os.environ.get("VROXY_DISPATCH_STATE_DIR",
                                      str(Path.home() / ".cache" / "vroxy-dispatch")))
RESTART_NOTICE_PATH = STATE_DIR / "restart-notice.json"
# Work that was queued or in flight when the process died.  The queue
# lives in memory and the server broadcasts each message exactly once,
# so anything still on it when systemd stops the unit is gone with no
# trace — the asker just never hears back.
WORK_SPOOL_PATH = STATE_DIR / "work-spool.json"
WORK_SPOOL_MAX = 50
# Replaying a question from an hour ago is worse than dropping it:
# the answer arrives with no context and the asker has moved on.
WORK_SPOOL_MAX_AGE_SECONDS = 1_800
# A notice older than this belongs to a restart nobody is still
# waiting on — announcing it would be confusing, not informative.
RESTART_NOTICE_MAX_AGE_SECONDS = 900

CHANNEL_IDENTIFIER = json.dumps({"channel": "AdminFeedbackChannel"})
AGENT_VERSION      = "vroxy_dispatch 0.25.0"
HEARTBEAT_INTERVAL_SECONDS = 20
# Rails caps a RoomMessage body at RoomMessage::BODY_MAX; the server
# truncates too, but splitting here keeps whole sentences.
ROOM_BODY_MAX = 4_000

ROOM_LOG_BODY_MAX = 1_000
STALL_SECONDS               = int(os.environ.get("VROXY_STALL_SECONDS", "90"))
STALL_WINDOWS_BEFORE_KILL   = int(os.environ.get("VROXY_STALL_WINDOWS", "4"))
SUBPROCESS_HARD_CAP_SECONDS = STALL_SECONDS * STALL_WINDOWS_BEFORE_KILL

# Mutable status reported on each heartbeat.  Updated by the worker
# as it moves through feedback / apply lifecycles so the admin UI
# can show "processing feedback abc123" in real time.
_current_status: str = "idle"

# Logs go to the terminal AND to a file, so a run started in a
# shell is still readable (`tail -f`) after that shell is gone.
# LOG_FILE overrides the path; LOG_FILE="" disables file logging.
LOG_FILE = os.environ.get("LOG_FILE",
                          str(Path(__file__).resolve().parent / "log" / "dispatch.log"))
LOG_MAX_BYTES    = int(os.environ.get("LOG_MAX_BYTES", 10 * 1024 * 1024))
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", 5))


TASK_LABEL_MAX = 56

_task_label: contextvars.ContextVar[str] = contextvars.ContextVar("task_label", default="")


def set_task_label(label: str) -> None:
    _task_label.set(_one_line(label, TASK_LABEL_MAX) if label else "")


def task_label() -> str:
    return _task_label.get() or "idle"


class _TaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.task = task_label()
        return True


def _setup_logging() -> logging.Logger:
    fmt   = logging.Formatter("%(asctime)s %(levelname)s [%(task)s] %(message)s")
    level = os.environ.get("LOG_LEVEL", "INFO").upper()

    root = logging.getLogger()
    root.setLevel(level)
    root.addFilter(_TaskFilter())

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.addFilter(_TaskFilter())
    root.addHandler(stream)

    if LOG_FILE:
        try:
            path = Path(LOG_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            rotating = RotatingFileHandler(
                path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8")
            rotating.setFormatter(fmt)
            rotating.addFilter(_TaskFilter())
            root.addHandler(rotating)
        except OSError as e:
            # An unwritable log path must never stop dispatch from
            # running — the terminal handler is already attached.
            root.warning("file logging disabled (%s): %s", LOG_FILE, e)

    return logging.getLogger("vroxy_dispatch")


log = _setup_logging()


# ── The cable link ────────────────────────────────────────────────
class CableLink:
    """The socket, decoupled from its lifetime.

    A run takes minutes; a deploy drops the cable in the middle of one.
    Handlers used to hold the websocket they started with, so a
    reconnect left them writing into a dead socket — the Claude run
    finished, spent its tokens, and the answer went nowhere.

    Handlers hold THIS instead.  It swaps the underlying socket on
    reconnect, and buffers anything sent while there isn't one so the
    result still lands once the server comes back."""

    # A deploy is seconds, not hours.  Bounded so a genuinely dead
    # server can't grow this without limit; oldest go first because a
    # stale progress chip matters less than a fresh reply.
    MAX_OUTBOX = 200

    def __init__(self):
        self.ws = None
        self.outbox: list[str] = []

    def attach(self, ws) -> None:
        self.ws = ws

    def detach(self) -> None:
        self.ws = None

    async def send(self, frame: str, buffer: bool = True) -> None:
        """`buffer=False` for frames that are only true right now — a
        heartbeat replayed after a reconnect reports a stale status as
        current, which is worse than the gap it was covering."""
        if self.ws is None:
            if buffer:
                self._buffer(frame)
            return
        try:
            await self.ws.send(frame)
        except Exception as e:
            log.warning("send failed (%s) — %s", e,
                        "buffering for the next connection" if buffer else "dropping")
            self.ws = None
            if buffer:
                self._buffer(frame)

    def _buffer(self, frame: str) -> None:
        if len(self.outbox) >= self.MAX_OUTBOX:
            dropped = self.outbox.pop(0)
            log.warning("outbox full — dropped the oldest frame (%.80s)", dropped)
        self.outbox.append(frame)

    async def flush(self) -> None:
        """Send everything that piled up while the cable was down.
        Anything that fails is re-buffered by `send`."""
        if not self.outbox:
            return
        pending, self.outbox = self.outbox, []
        log.info("flushing %d buffered frame(s)", len(pending))
        for frame in pending:
            await self.send(frame)


# ── ActionCable helpers ───────────────────────────────────────────
async def cable_send(ws, command: str, data: dict | None = None,
                     buffer: bool = True) -> None:
    """One ActionCable frame.  `command` is 'subscribe' or
    'message'; `data` is the per-command payload."""
    frame: dict[str, Any] = {"command": command, "identifier": CHANNEL_IDENTIFIER}
    if data is not None:
        frame["data"] = json.dumps(data)
    payload = json.dumps(frame)
    if isinstance(ws, CableLink):
        await ws.send(payload, buffer=buffer)
    else:
        await ws.send(payload)


async def subscribe(ws) -> None:
    # Belongs to the socket that asked for it; replaying an old
    # subscribe onto a new connection is meaningless.
    await cable_send(ws, "subscribe", buffer=False)


# Every coding CLI this dispatch knows how to look for.  `engine` is
# the DISPATCH_ENGINE value that runs it, or None for one we can only
# report on — knowing Gemini is installed is useful to an operator
# deciding what to configure even before we can drive it.
KNOWN_HARNESSES = (
    {"id": "claude", "label": "Claude Code",   "bin": "claude",       "engine": "claude"},
    {"id": "codex",  "label": "OpenAI Codex",  "bin": "codex",        "engine": "codex"},
    {"id": "gemini", "label": "Gemini CLI",    "bin": "gemini",       "engine": None},
    {"id": "copilot", "label": "GitHub Copilot CLI", "bin": "copilot", "engine": None},
    {"id": "aider",  "label": "Aider",         "bin": "aider",        "engine": None},
    {"id": "opencode", "label": "OpenCode",    "bin": "opencode",     "engine": None},
    {"id": "pi",     "label": "Pi",            "bin": "pi",           "engine": None},
    {"id": "cursor", "label": "Cursor Agent",  "bin": "cursor-agent", "engine": None},
    {"id": "amp",    "label": "Amp",           "bin": "amp",          "engine": None},
    {"id": "goose",  "label": "Goose",         "bin": "goose",        "engine": None},
)

# A `--version` that hangs must not hold the heartbeat open, and the
# whole sweep has to stay far under STALL_SECONDS — nine probes at
# five seconds is the worst case and it is still well inside it.
HARNESS_PROBE_TIMEOUT  = 5
HARNESS_REPROBE_SECONDS = 900

_harness_cache: tuple[float, list] | None = None


def _harness_version(path: str) -> str | None:
    """`<bin> --version`, first line, or None if it won't answer."""
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            timeout=HARNESS_PROBE_TIMEOUT,
        )
    except Exception:
        return None
    text = (out.stdout or out.stderr or "").strip()
    if not text:
        return None
    return _one_line(text.splitlines()[0], 60)


def probe_harnesses() -> list[dict]:
    """Which coding CLIs are installed on this box.

    Reported so an operator can see what a checkout could be driven
    with without shelling into it.  An entry that resolves but won't
    report a version is still listed — installed-but-broken is a
    different problem from not installed, and flattening the two into
    "absent" hides the one worth fixing.
    """
    from shutil import which
    found = []
    for h in KNOWN_HARNESSES:
        override = os.environ.get(f"{h['id'].upper()}_BIN")
        path = override if override and Path(override).exists() else which(h["bin"])
        if not path:
            continue
        entry = {"id": h["id"], "label": h["label"]}
        version = _harness_version(path)
        if version:
            entry["version"] = version
        if h["engine"]:
            entry["engine"] = h["engine"]
        entry["active"] = h["engine"] == DISPATCH_ENGINE
        found.append(entry)
    return found


def harnesses(now: float | None = None) -> list[dict]:
    """Cached [probe_harnesses], refreshed every
    HARNESS_REPROBE_SECONDS so a CLI installed while dispatch runs
    turns up without a restart, without spawning nine subprocesses on
    every 20-second heartbeat."""
    global _harness_cache
    now = time.monotonic() if now is None else now
    if _harness_cache and (now - _harness_cache[0]) < HARNESS_REPROBE_SECONDS:
        return _harness_cache[1]
    found = probe_harnesses()
    _harness_cache = (now, found)
    log.info("harnesses on this box: %s",
             ", ".join(f"{h['id']}={h.get('version', '?')}" for h in found) or "none")
    return found


async def heartbeat(ws) -> None:
    """Heartbeat frame.  AdminFeedbackChannel#heartbeat writes it
    into Rails.cache under a tenant-scoped key with a 60 s TTL —
    the admin index card polls that key to show 🟢/🔴 + version."""
    meta = {"status": _current_status, "project": PROJECT,
            "engine": DISPATCH_ENGINE,
            # Probing blocks on subprocesses, so it never runs on the
            # event loop — a slow `--version` would stall the socket.
            "harnesses": await asyncio.to_thread(harnesses)}
    # Only sent when configured — an install that predates install.sh
    # keeps the old single-agent resolution rather than registering a
    # duplicate under a name nobody chose.
    if INSTALL_ID:
        meta["install_id"] = INSTALL_ID
    if AGENT_NAME:
        meta["agent_name"] = AGENT_NAME
    await cable_send(ws, "message", {
        "action":  "heartbeat",
        "version": AGENT_VERSION,
        "meta":    meta,
    }, buffer=False)


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
                proposal: dict | None = None,
                pull_request: dict | None = None) -> None:
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
    if pull_request is not None:
        payload["pull_request"] = pull_request
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


ROOM_REPLY_ACK_TIMEOUT_SECONDS = 10

_posted_message_acks: dict[str, asyncio.Queue] = {}


def note_posted_message(room_id: str, message_id: str) -> None:
    """Records the `room_reply.posted` acknowledgement the server
    transmits to this subscriber alone, naming the message it just
    created.  Dropped when no turn is waiting on that room."""
    queue = _posted_message_acks.get(str(room_id or ""))
    if queue is None or not message_id:
        return
    queue.put_nowait(str(message_id))


async def post_room_reply(ws, room_id: str, chunks: list[str],
                          reply_to: str | None = None) -> str | None:
    """Posts every chunk of an answer and returns the hashid of the
    LAST one, which is the only message an ask may hang off.

    Each chunk waits for its own acknowledgement before the next goes
    out, so the hashid returned belongs to the chunk that ends the
    reply.  A server that never answers costs one timeout, not one per
    chunk, and costs the ask rather than the reply."""
    queue: asyncio.Queue = asyncio.Queue()
    _posted_message_acks[room_id] = queue
    posted: str | None = None
    expect_acks = True
    try:
        for chunk in chunks:
            while not queue.empty():
                queue.get_nowait()
            await room_reply(ws, room_id, chunk, reply_to)
            if not expect_acks:
                continue
            try:
                posted = await asyncio.wait_for(queue.get(),
                                                ROOM_REPLY_ACK_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("No room_reply.posted for room=%s in %ss — "
                            "the reply stands, the ask is dropped",
                            room_id, ROOM_REPLY_ACK_TIMEOUT_SECONDS)
                posted, expect_acks = None, False
    finally:
        _posted_message_acks.pop(room_id, None)
    return posted


async def room_ask(ws, room_id: str, message_id: str | None, ask: dict | None) -> None:
    """`AdminFeedbackChannel#room_ask` — turns the question into
    buttons under the message it names.  No message to hang it on
    means no question; the prose fallback already carries it."""
    if not message_id or not ask:
        return
    await cable_send(ws, "message", {
        "action":     "room_ask",
        "room_id":    room_id,
        "message_id": message_id,
        "prompt":     ask["prompt"],
        "mode":       ask["mode"],
        "options":    ask["options"],
    })
    log.info("Room ask sent room=%s message=%s mode=%s options=%d",
             room_id, message_id, ask["mode"], len(ask["options"]))


async def room_status(ws, room_id: str, reply_to: str | None, state: str) -> None:
    """`queued` when it lands on the work queue, `working` when the
    worker picks it up.  One in-flight run at a time means a request
    can sit for twenty minutes before anything happens, and a room
    with no signal at all is indistinguishable from one where the
    message never arrived."""
    if not reply_to:
        return
    await cable_send(ws, "message", {
        "action": "room_status", "room_id": room_id,
        "reply_to": reply_to, "state": state,
    })




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

3. Mode.  You do NOT decide how this ships — the workspace's
   dispatch policy does that server-side, based on the size and
   shape of your change.  Leave `mode` as `inline_ship` unless the
   note EXPLICITLY asks for a pull request ("PR", "pull request",
   "branch", "for review", "don't ship yet"), in which case set
   `pull_request` and the policy will honor it.

   If the feedback is genuinely ambiguous or you can't do it
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

NEVER BLOCK LONGER THAN 90 SECONDS ON ONE COMMAND.  Someone is
watching a typing indicator while you work, so a wait they can't see
the end of is the worst thing you can do to them.

- Every Bash call you make gets `timeout: 90000` or less.  If a
  command needs longer than that, you are running the wrong command.
- Run the tests that cover your change — the specific files — never
  the whole suite.  No `bin/system-test` with no argument, no bare
  `bash ./test.sh`, no full `bin/test`.  CI runs everything on push;
  a local full pass buys you nothing but the user's patience.
- When something genuinely has to take minutes (a full suite you were
  ASKED for, a build, waiting on a deploy), do NOT raise the timeout.
  Background it and poll:

      nohup bin/test test/models/foo_test.rb > /tmp/t.log 2>&1 &

  then come back to `tail /tmp/t.log` between other work.  Do real
  work in the gaps; never sleep waiting.
- If you hit the ceiling anyway, say so in the room and move on to
  the next thing rather than retrying the same long command.
- When that question has a small set of answers, ask it in your prose
  AND end the reply with one fenced `ask` block — the room turns it
  into buttons:

  ```ask
  {"prompt": "Ship this to master or open a PR?",
   "mode": "one",
   "options": ["Ship to master", "Open a PR"]}
  ```

  `mode` is `one` (pick one), `many` (pick several), or `text` (a
  free-text box, `options` omitted).  Twelve options at most, one
  block per reply, last thing in the message.  Keep asking in the
  prose too: not every surface draws the buttons.

The conversation so far is below.  Reply with only your message —
no "Dispatch:" prefix, no fenced proposal block.
""".strip()


# ── Attachments ───────────────────────────────────────────────────
# A screenshot is usually the whole point of the message ("why does
# this look wrong?"), and Claude can only look at a file that exists
# on disk.  The envelope carries a fetch URL per attachment; we pull
# each one down beside the session cache — never into the checkout,
# which would show up as untracked junk in the very diff we're about
# to propose — and hand the model absolute paths.
ATTACHMENT_DIR       = SID_DIR.parent / "vroxy-attachments"
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
ATTACHMENT_MAX_COUNT = 5
ATTACHMENT_TIMEOUT   = 20


def _app_base_url() -> str:
    """`https://host[:port]` for the app behind CABLE_URL.  A
    bucket-less dev server serves uploads as a relative path, so the
    envelope's URL needs a host bolted on; the cable's is the one host
    we know is right."""
    parsed = urllib.parse.urlparse(CABLE_URL)
    scheme = "https" if parsed.scheme == "wss" else "http"
    port   = f":{parsed.port}" if parsed.port else ""
    return f"{scheme}://{parsed.hostname}{port}"


def _safe_attachment_name(name: str, fallback: str) -> str:
    """Filenames come from whoever uploaded them.  Keep the basename,
    keep it boring, and never let it climb out of the directory."""
    base = Path(str(name or "")).name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    return base[:120] or fallback


def download_attachments(msg: dict) -> list[dict]:
    """Fetch the message's attachments to disk.  Returns the ones that
    landed, each with a `path`.  Best-effort per file: a screenshot we
    couldn't fetch must not cost the room its answer."""
    items = msg.get("attachments") or []
    if not items:
        return []

    hashid = str(msg.get("hashid") or "msg")
    target_dir = ATTACHMENT_DIR / _safe_attachment_name(hashid, "msg")
    got: list[dict] = []

    for i, item in enumerate(items[:ATTACHMENT_MAX_COUNT]):
        url = (item.get("url") or "").strip()
        if not url:
            continue
        if url.startswith("/"):
            url = _app_base_url() + url
        if not url.startswith(("http://", "https://")):
            log.warning("skipping attachment with odd URL scheme: %.60s", url)
            continue

        size = item.get("byte_size") or 0
        if size and size > ATTACHMENT_MAX_BYTES:
            log.warning("skipping attachment %s — %s bytes over the cap",
                        item.get("filename"), size)
            continue

        name = _safe_attachment_name(item.get("filename"), f"attachment-{i + 1}")
        dest = target_dir / name
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=ATTACHMENT_TIMEOUT) as resp:
                data = resp.read(ATTACHMENT_MAX_BYTES + 1)
            if len(data) > ATTACHMENT_MAX_BYTES:
                log.warning("skipping attachment %s — body over the cap", name)
                continue
            dest.write_bytes(data)
        except Exception as e:
            log.warning("attachment download failed (%s): %s: %s", name, type(e).__name__, e)
            continue

        got.append({**item, "path": str(dest), "bytes": len(data)})
        log.info("Attachment saved %s (%d bytes)", dest, len(data))

    return got


def build_room_prompt(payload: dict, attachments: list[dict] | None = None) -> str:
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

    if attachments:
        lines.append("")
        lines.append("## Attachments on that message")
        lines.append("Already downloaded — read them with the Read tool; "
                     "a screenshot is usually the point of the message.")
        for a in attachments:
            kind = a.get("kind") or "file"
            size = a.get("bytes") or a.get("byte_size") or 0
            lines.append(f"- `{a['path']}` — {a.get('filename') or '?'} "
                         f"({kind}, {size} bytes)")

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

    lines = [SYSTEM_PROMPT, ""]

    # How this workspace ships, so the model knows what will happen
    # to its work rather than inferring it from the note's wording.
    policy = payload.get("policy") or {}
    if policy:
        lines.append("── Ship policy (set by the operator, not by you) ──")
        lines.append("")
        described = {
            "always_ship": "every approved change commits straight to the base branch",
            "always_pr":   "every approved change becomes a pull request for review",
            "auto":        "small changes commit to the base branch; larger ones become a PR",
        }.get(str(policy.get("policy")), "decided server-side")
        lines.append(f"- Workspace policy: **{policy.get('policy')}** — {described}.")
        lines.append(f"- Base branch: `{policy.get('base_ref')}`")
        if policy.get("auto_apply"):
            lines.append("- Auto-apply is ON: a change the policy sizes as small ships "
                         "WITHOUT a human clicking approve. Be correspondingly careful.")
        lines.append("")

    lines += ["── Feedback ──", "", f"**Note:** {note}", ""]

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


@contextlib.contextmanager
def proposal_worktree(project_dir: Path):
    """A throwaway checkout of HEAD for the PROPOSAL phase, so an
    investigation that decides to edit files never touches the real
    working tree.  "Pending review" is only honest if nothing has
    been applied yet.

    WHERE it lives matters.  The worktree is created as a SIBLING of
    the project inside CODE_ROOT, not in /tmp, because that's what
    keeps a Claude run's context identical to a human's in this
    workspace: the parent CODE_ROOT/CLAUDE.md still loads (it's a
    parent directory of the worktree), and sibling repos still
    resolve at `../vroxy_dispatch`, `../vroxy_mobile`, and so on.
    A /tmp worktree silently loses both.

    Yields the worktree path, and removes it on the way out.  Stale
    worktrees from a killed run are pruned on entry."""
    _run(["git", "worktree", "prune"], project_dir)
    name = f".dispatch-{project_dir.name}-{uuid.uuid4().hex[:8]}"
    path = project_dir.parent / name

    rc, _, err = _run(["git", "worktree", "add", "--detach", str(path), "HEAD"], project_dir)
    if rc != 0:
        raise RuntimeError(f"could not create worktree at {path}: {err[:300]}")
    log.info("Proposal worktree %s", path)
    try:
        yield path
    finally:
        rc, _, err = _run(["git", "worktree", "remove", "--force", str(path)], project_dir)
        if rc != 0:
            log.warning("worktree remove failed (%s) — pruning", err[:200])
            _run(["git", "worktree", "prune"], project_dir)


def head_sha(project_dir: Path) -> str:
    """The commit a proposal was generated against.  Sent with the
    proposal so the server can refuse to apply one whose base has
    moved on — file contents are a snapshot, and applying a snapshot
    of an old tree silently reverts whatever landed since."""
    rc, out, _ = _run(["git", "rev-parse", "HEAD"], project_dir)
    return (out or "").strip() if rc == 0 else ""


def worktree_diffstat(worktree: Path) -> dict:
    """`git diff --numstat HEAD` in the worktree — an exact count of
    what the proposal actually changes, which is what the server's
    ship policy sizes on.  Free only because the run was isolated."""
    rc, out, _ = _run(["git", "diff", "--numstat", "HEAD"], worktree)
    if rc != 0:
        return {}
    files = insertions = deletions = 0
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        # Binary files report "-" for both counts.
        if parts[0] != "-":
            insertions += int(parts[0] or 0)
        if parts[1] != "-":
            deletions += int(parts[1] or 0)
    return {"files": files, "insertions": insertions, "deletions": deletions}


def worktree_changed_files(worktree: Path) -> list[dict]:
    """Every file the run modified or added, as proposal entries.
    Used when Claude edited the tree but didn't emit a fenced
    proposal — the work is on disk either way and throwing it away
    would be worse than shipping a proposal it didn't format."""
    rc, out, _ = _run(["git", "diff", "--name-only", "HEAD"], worktree)
    if rc != 0:
        return []
    rc2, untracked, _ = _run(["git", "ls-files", "--others", "--exclude-standard"], worktree)
    names = [n for n in (out or "").splitlines() if n.strip()]
    names += [n for n in (untracked or "").splitlines() if n.strip()]

    files = []
    for name in dict.fromkeys(names):
        target = worktree / name
        try:
            if target.is_file():
                files.append({"path": name, "content": target.read_text()})
        except (OSError, UnicodeDecodeError):
            log.warning("skipping unreadable/binary file in proposal: %s", name)
    return files


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



def _resolve_codex_bin() -> str:
    """$CODEX_BIN → PATH → ~/.local/bin/codex → /usr/local/bin/codex.
    Fail-loud for the same reason `claude` does: a missing binary must
    not read as an agent that had nothing to say."""
    override = os.environ.get("CODEX_BIN")
    if override:
        return override
    from shutil import which
    on_path = which("codex")
    if on_path:
        return on_path
    for guess in (Path.home() / ".local/bin/codex", Path("/usr/local/bin/codex")):
        if guess.is_file() and os.access(guess, os.X_OK):
            return str(guess)
    raise FileNotFoundError("`codex` binary not found. Set CODEX_BIN or put it on PATH.")


_STALLED = object()


def _stream_lines(proc):
    """Yield each stdout line of `proc`, and `_STALLED` once per
    STALL_SECONDS that pass without one.

    Iterating `proc.stdout` directly blocks with no way out: a claude
    run that wedges — a hung tool call, a dead network read — held
    dispatch open forever with a typing indicator and no answer.  A
    daemon thread does the blocking read so the caller only ever waits
    on a queue it can time out."""
    lines: SimpleQueue = SimpleQueue()

    def pump():
        try:
            for raw in proc.stdout:
                lines.put(raw)
        except Exception:
            log.exception("stdout pump raised")
        finally:
            lines.put(None)

    reader = threading.Thread(target=pump, name="claude-stdout", daemon=True)
    reader.start()
    while True:
        try:
            item = lines.get(timeout=STALL_SECONDS)
        except Empty:
            yield _STALLED
            continue
        if item is None:
            return
        yield item


def _kill_process_tree(proc) -> None:
    """SIGTERM the whole group, then SIGKILL what ignored it.  Claude
    spawns its tools as children; killing only the parent leaves a
    wedged `bin/test` holding the database."""
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def run_claude_streamed(prompt: str, project: str, on_event,
                        allow_resume: bool = True,
                        session_key: str | None = None,
                        work_dir_override: Path | None = None) -> str:
    """`claude -p ... --output-format stream-json` variant.

    Emits each JSON event to `on_event(dict)` as it arrives so
    callers can forward tool_use / text events into a chat widget
    live.  Blocking; call from a thread.  Returns aggregated final
    result text.

    A stored session id can outlive the transcript it points at (a
    cleared history, a different host, `~/.claude` wiped).  `--resume`
    then exits 1 having produced nothing, so on that signature the
    session file is dropped and the prompt retried fresh once."""
    work_dir = str(work_dir_override or (CODE_ROOT / project))
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
        start_new_session=True,
    )

    final_text_chunks: list[str] = []
    new_sid: str | None = None

    # Compact tally so the log summary at end shows what Claude did.
    tool_calls = 0
    text_chars = 0
    thinking_chars = 0
    stalled_windows = 0
    killed_for_stall = False

    try:
        for raw in _stream_lines(proc):
            if raw is _STALLED:
                stalled_windows += 1
                waited = stalled_windows * STALL_SECONDS
                log.warning("claude produced nothing for %ss (window %s/%s)",
                            waited, stalled_windows, STALL_WINDOWS_BEFORE_KILL)
                try:
                    on_event({"type": "stalled", "seconds": waited,
                              "window": stalled_windows,
                              "max_windows": STALL_WINDOWS_BEFORE_KILL})
                except Exception:
                    log.exception("on_event stalled raised")
                if stalled_windows >= STALL_WINDOWS_BEFORE_KILL:
                    killed_for_stall = True
                    log.error("killing wedged claude run after %ss of silence", waited)
                    _kill_process_tree(proc)
                    break
                continue
            stalled_windows = 0
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
                            try:
                                on_event({"type": "thinking", "text": thought})
                            except Exception:
                                log.exception("on_event thinking raised")

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
                # The CLI is the only thing that knows what the run
                # actually cost — cache reads and writes price nothing
                # like base input, so this is reported, never derived.
                try:
                    on_event({
                        "type":        "result",
                        "usage":       usage,
                        "cost_usd":    event.get("total_cost_usd"),
                        "duration_ms": event.get("duration_ms"),
                        "num_turns":   event.get("num_turns"),
                        "is_error":    bool(event.get("is_error")),
                    })
                except Exception:
                    log.exception("on_event result raised")

        try:
            proc.wait(timeout=STALL_SECONDS)
        except subprocess.TimeoutExpired:
            log.error("claude closed stdout but would not exit — killing")
            _kill_process_tree(proc)
    finally:
        if proc.stdout: proc.stdout.close()
        stderr_text = ""
        if proc.stderr:
            if proc.poll() is None:
                _kill_process_tree(proc)
            stderr_text = proc.stderr.read() or ""
            if stderr_text:
                log.info("claude stderr: %s", stderr_text[:400])
            proc.stderr.close()

    failed = proc.returncode not in (0, None)
    if failed:
        log.warning("streamed claude rc=%s", proc.returncode)

    produced_nothing = not final_text_chunks and tool_calls == 0

    if killed_for_stall:
        waited = STALL_SECONDS * STALL_WINDOWS_BEFORE_KILL
        reason = (f"the run produced nothing for {waited}s, so I killed it as "
                  f"wedged rather than leave you waiting")
        if produced_nothing:
            raise RuntimeError(reason)
        log.warning("returning partial output from a stalled run")
        partial = "".join(final_text_chunks).strip()
        return f"{partial}\n\n⚠️ Cut short — {reason}.".strip()

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


def run_codex_streamed(prompt: str, project: str, on_event,
                       allow_resume: bool = True,
                       session_key: str | None = None,
                       work_dir_override: Path | None = None) -> str:
    """`codex exec --json` variant of [run_claude_streamed].

    Same contract on purpose: same signature, the same normalised
    event vocabulary out of `on_event` (`tool_use` / `text_delta` /
    `thinking` / `result` / `stalled`), the same stall kill, the same
    stale-session retry, the same return of the final answer text.
    Everything downstream — ProgressTrail, the room reply, the
    proposal flow — is engine-agnostic because of that and needs no
    branch of its own.

    Codex speaks a different stream: `thread.started` carries the id
    to resume, work arrives as `item.started` / `item.completed` with
    an `item.type`, and the turn closes with `turn.completed`."""
    work_dir = str(work_dir_override or (CODE_ROOT / project))
    if not Path(work_dir).is_dir():
        raise FileNotFoundError(
            f"CODE_ROOT/{project} not found at {work_dir!r}. "
            f"Set CODE_ROOT and/or PROJECT env vars — CODE_ROOT currently = {CODE_ROOT!r}"
        )
    codex_bin = _resolve_codex_bin()
    sid_file = _streamed_sid_file(f"codex_{session_key or project}")
    sid_file.parent.mkdir(parents=True, exist_ok=True)

    stored_sid = sid_file.read_text().strip() if sid_file.exists() else ""
    resumed = bool(allow_resume and stored_sid)

    # The worktree this runs in is already disposable and already the
    # trust boundary, exactly as it is for claude's
    # --dangerously-skip-permissions.  Codex additionally cannot nest
    # its own sandbox inside the one this box already runs under, so
    # without the bypass every shell call comes back "Operation not
    # permitted" and the model reports failure instead of working.
    flags = ["--json", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox",
             "-C", work_dir]
    if resumed:
        argv = [codex_bin, "exec", "resume", stored_sid] + flags + [prompt]
    else:
        argv = [codex_bin, "exec"] + flags + [prompt]

    log.info("Running streamed codex in %s (session=%s, resume=%s)",
             work_dir, sid_file, resumed)
    proc = subprocess.Popen(
        argv, cwd=work_dir, env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        start_new_session=True,
    )

    messages: list[str] = []
    new_sid: str | None = None
    usage: dict = {}
    turn_failed = False
    tool_calls = 0
    text_chars = 0
    thinking_chars = 0
    stalled_windows = 0
    killed_for_stall = False

    try:
        for raw in _stream_lines(proc):
            if raw is _STALLED:
                stalled_windows += 1
                waited = stalled_windows * STALL_SECONDS
                log.warning("codex produced nothing for %ss (window %s/%s)",
                            waited, stalled_windows, STALL_WINDOWS_BEFORE_KILL)
                try:
                    on_event({"type": "stalled", "seconds": waited,
                              "window": stalled_windows,
                              "max_windows": STALL_WINDOWS_BEFORE_KILL})
                except Exception:
                    log.exception("on_event stalled raised")
                if stalled_windows >= STALL_WINDOWS_BEFORE_KILL:
                    killed_for_stall = True
                    log.error("killing wedged codex run after %ss of silence", waited)
                    _kill_process_tree(proc)
                    break
                continue
            stalled_windows = 0
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "thread.started":
                tid = event.get("thread_id")
                if tid:
                    new_sid = str(tid)
                continue

            if etype in ("item.started", "item.completed"):
                item = event.get("item") or {}
                itype = item.get("type")

                if itype == "command_execution":
                    # Announced on start, matching claude's tool_use
                    # timing — the room should see the command as it
                    # runs, not once it has already finished.
                    if etype == "item.started":
                        tool_calls += 1
                        command = item.get("command") or ""
                        log.info("  → command %s", _one_line(command, 200))
                        try:
                            on_event({"type": "tool_use", "name": "Bash",
                                      "input": {"command": command}})
                        except Exception:
                            log.exception("on_event tool_use raised")
                    else:
                        exit_code = item.get("exit_code")
                        marker = "ok" if exit_code == 0 else f"⚠️ exit {exit_code}"
                        log.info("  ← command %s %s", marker,
                                 _one_line(item.get("aggregated_output") or "", 160))
                    continue

                if etype != "item.completed":
                    continue

                if itype == "agent_message":
                    text = item.get("text") or ""
                    if text:
                        text_chars += len(text)
                        messages.append(text)
                        log.info("  → text %s", _one_line(text, 200))
                        try:
                            on_event({"type": "text_delta", "text": text})
                        except Exception:
                            log.exception("on_event text raised")
                    continue

                if itype == "reasoning":
                    thought = item.get("text") or item.get("summary") or ""
                    if thought:
                        thinking_chars += len(thought)
                        log.info("  · thinking %s", _one_line(thought, 200))
                        try:
                            on_event({"type": "thinking", "text": thought})
                        except Exception:
                            log.exception("on_event thinking raised")
                    continue

                # Any other item kind (file_change, mcp_tool_call,
                # web_search, …) still reads as work happening, which
                # is what the trail is for.  Reporting it by its own
                # name beats dropping it because this build predates
                # it.
                if itype:
                    tool_calls += 1
                    try:
                        on_event({"type": "tool_use", "name": str(itype),
                                  "input": {k: v for k, v in item.items()
                                            if k not in ("id", "type")}})
                    except Exception:
                        log.exception("on_event tool_use raised")
                continue

            if etype in ("turn.completed", "turn.failed"):
                turn_failed = turn_failed or etype == "turn.failed"
                usage = event.get("usage") or usage
                if usage:
                    log.info("  · usage %s", usage)
                try:
                    on_event({
                        "type":        "result",
                        "usage":       usage,
                        # Codex bills against the signed-in plan and
                        # reports no per-run price, so this is null
                        # rather than a number we made up.
                        "cost_usd":    None,
                        "duration_ms": None,
                        "num_turns":   None,
                        "is_error":    turn_failed,
                    })
                except Exception:
                    log.exception("on_event result raised")

        try:
            proc.wait(timeout=STALL_SECONDS)
        except subprocess.TimeoutExpired:
            log.error("codex closed stdout but would not exit — killing")
            _kill_process_tree(proc)
    finally:
        if proc.stdout: proc.stdout.close()
        stderr_text = ""
        if proc.stderr:
            if proc.poll() is None:
                _kill_process_tree(proc)
            stderr_text = proc.stderr.read() or ""
            if stderr_text:
                log.info("codex stderr: %s", stderr_text[:400])
            proc.stderr.close()

    failed = turn_failed or proc.returncode not in (0, None)
    if failed:
        log.warning("streamed codex rc=%s turn_failed=%s", proc.returncode, turn_failed)

    # The LAST agent message is the answer; the ones before it are
    # narration the trail has already shown.  Joining them all would
    # repeat the commentary inside the reply.
    final_text = messages[-1].strip() if messages else ""
    produced_nothing = not final_text and tool_calls == 0

    if killed_for_stall:
        waited = STALL_SECONDS * STALL_WINDOWS_BEFORE_KILL
        reason = (f"the run produced nothing for {waited}s, so I killed it as "
                  f"wedged rather than leave you waiting")
        if produced_nothing:
            raise RuntimeError(reason)
        log.warning("returning partial output from a stalled codex run")
        return f"{final_text}\n\n⚠️ Cut short — {reason}.".strip()

    if resumed and failed and produced_nothing:
        log.warning("codex resume produced nothing (rc=%s) — dropping stale "
                    "thread id and retrying fresh", proc.returncode)
        try: sid_file.unlink(missing_ok=True)
        except Exception: log.exception("clearing thread id failed")
        return run_codex_streamed(prompt, project, on_event,
                                  allow_resume=False, session_key=session_key,
                                  work_dir_override=work_dir_override)

    if new_sid and not failed:
        try: sid_file.write_text(new_sid)
        except Exception: log.exception("saving thread id failed")

    log.info("streamed codex done — tool_calls=%d text_chars=%d thinking_chars=%d",
             tool_calls, text_chars, thinking_chars)

    if failed and produced_nothing:
        raise RuntimeError(
            f"codex exited {proc.returncode} with no output"
            + (f": {_one_line(stderr_text, 300)}" if stderr_text else ""))

    return final_text


def run_agent_streamed(prompt: str, project: str, on_event,
                       allow_resume: bool = True,
                       session_key: str | None = None,
                       work_dir_override: Path | None = None) -> str:
    """Run whichever engine `DISPATCH_ENGINE` selects.

    The only place in the process that knows there is more than one."""
    if DISPATCH_ENGINE == "codex":
        runner = run_codex_streamed
    elif DISPATCH_ENGINE == "claude":
        runner = run_claude_streamed
    else:
        raise ValueError(
            f"DISPATCH_ENGINE={DISPATCH_ENGINE!r} is not a known engine "
            f"(expected 'claude' or 'codex')")
    return runner(prompt, project, on_event, allow_resume, session_key,
                  work_dir_override)


class ProgressTrail:
    """Turns a Claude event stream into the lines a room should see.

    The subtle one is TEXT. A text block is narration when more work
    follows it and the ANSWER when nothing does — and which it is
    can't be known until the next event arrives. So text is HELD:
    flushed as a trail line when a tool call or a thought comes next,
    dropped when the run ends, because by then it is the reply and
    saying it twice helps nobody.

    Before this, text was dropped either way, which threw away the
    most readable part of a run — "Now the view marker, the copy-link
    action, and the JS" tells you more at a glance than
    `Bash(python3 - <<PY …)`.
    """

    def __init__(self, emit, text_max: int = 400):
        self._emit = emit
        self._text_max = text_max
        self._held: list[str] = []

    def feed(self, event: dict) -> None:
        etype = event.get("type")

        if etype == "result":
            self._held.clear()
            return

        if etype == "text_delta":
            # Consecutive text with no work between it is one block of
            # prose, not two lines.
            if event.get("text"):
                self._held.append(event["text"])
            return

        if etype == "tool_use":
            self._flush()
            self._emit("tool", _progress_line(event.get("name") or "tool", event.get("input")))
        elif etype == "thinking":
            self._flush()
            self._emit("thinking", _one_line(event.get("text") or "", self._text_max))
        elif etype == "stalled":
            self._flush()
            self._emit("stalled", _stall_line(event))

    def _flush(self) -> None:
        joined = " ".join(self._held).strip()
        self._held.clear()
        if joined:
            self._emit("text", _one_line(joined, self._text_max))


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


def run_claude(prompt: str, project: str, work_dir_override: Path | None = None) -> str:
    """Blocking subprocess to `claude-chat` — fallback path when
    the streamed run crashes (broken CLI flag on a Claude Code
    upgrade, etc.).  Called from a thread so the asyncio loop
    keeps ticking.

    There is no stream here to watch for silence, so this path gets
    the flat hard cap instead of the stall detector — which is the
    other reason the streamed path is the default."""
    work_dir = str(work_dir_override or (CODE_ROOT / project))
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
        timeout=SUBPROCESS_HARD_CAP_SECONDS,
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


# ── Self-update ───────────────────────────────────────────────────
# A run that edits this checkout leaves the process running code that
# is no longer on disk — every later answer comes from the old build
# while the heartbeat reports the new version number.  Dispatch
# notices and restarts itself.

# The files systemd actually executes.  Hashing them, rather than
# reading a version constant, catches a fix that landed without a
# version bump and an edit that was never committed.
_SELF_FILES = (Path(__file__).resolve(), Path(CLAUDE_CHAT_BIN))

_AGENT_VERSION_RE = re.compile(r'^AGENT_VERSION\s*=\s*"([^"]+)"', re.M)

_restart_pending: dict | None = None
_restart_scheduled = False


def source_fingerprint() -> str:
    h = hashlib.sha256()
    for path in _SELF_FILES:
        try:
            h.update(Path(path).read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


_BOOT_FINGERPRINT = source_fingerprint()


def self_updated() -> bool:
    return source_fingerprint() != _BOOT_FINGERPRINT


def disk_agent_version() -> str:
    """AGENT_VERSION as it reads on disk, which stops being
    `AGENT_VERSION` the moment a run edits this file."""
    try:
        text = _SELF_FILES[0].read_text(encoding="utf-8")
    except OSError:
        return ""
    found = _AGENT_VERSION_RE.search(text)
    return found.group(1) if found else ""


def self_compiles() -> tuple[bool, str]:
    """Byte-compile what's on disk before restarting into it.

    A restart into a SyntaxError is a crash loop: systemd restarts
    on failure, gives up after the start limit, and dispatch is off
    until a human notices.  Staying on the old build and saying so is
    strictly better than that."""
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", str(_SELF_FILES[0])],
        capture_output=True, text=True, timeout=60)
    return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()


def write_restart_notice(room_id: str | None, reply_to: str | None,
                         to_version: str) -> None:
    """What the NEXT process needs to know to report back.  In-memory
    state does not survive the restart it describes."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RESTART_NOTICE_PATH.write_text(json.dumps({
            "room_id":      room_id,
            "reply_to":     reply_to,
            "from_version": AGENT_VERSION,
            "to_version":   to_version,
            "at":           time.time(),
        }), encoding="utf-8")
    except OSError as e:
        log.warning("could not write the restart notice: %s", e)


def take_restart_notice() -> dict | None:
    """Read and DELETE.  Unlinked before the announcement is
    attempted, deliberately: every reconnect confirms the
    subscription again, and a notice that outlived one post would be
    re-announced on each of them."""
    try:
        raw = RESTART_NOTICE_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    with contextlib.suppress(OSError):
        RESTART_NOTICE_PATH.unlink()
    try:
        notice = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(notice, dict) or not notice.get("room_id"):
        return None
    age = time.time() - float(notice.get("at") or 0)
    if age > RESTART_NOTICE_MAX_AGE_SECONDS:
        log.warning("dropping a restart notice %.0fs old — nobody is still waiting", age)
        return None
    return notice


def schedule_restart() -> tuple[bool, str]:
    """Restart the unit from OUTSIDE our own cgroup.

    `systemctl restart` called from in here would work, but systemd
    stops the unit by killing its whole cgroup — including any Claude
    process still finishing, and including the shell that issued the
    command.  A transient timer owns it instead, so the restart
    survives us dying."""
    unit = f"vroxy-dispatch-self-restart-{uuid.uuid4().hex[:8]}"
    cmd = ["systemd-run", f"--on-active={RESTART_DELAY_SECONDS}s",
           f"--unit={unit}", "--collect",
           "systemctl", "restart", SERVICE_UNIT]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if proc.returncode == 0:
        return True, unit
    detail = (proc.stderr or proc.stdout or "").strip()
    log.warning("systemd-run failed (%s) — falling back to exiting", detail)
    return _exit_and_let_systemd_restart(detail)


def _exit_and_let_systemd_restart(reason: str) -> tuple[bool, str]:
    """Fallback: die non-zero and let the unit's own Restart= policy
    bring us back.  Only when that policy actually restarts on
    failure — exiting under `Restart=no` would take dispatch off the
    air until someone noticed, which is worse than running stale."""
    try:
        shown = subprocess.run(
            ["systemctl", "show", "-p", "Restart", "--value", SERVICE_UNIT],
            capture_output=True, text=True, timeout=10)
        policy = (shown.stdout or "").strip()
    except Exception:
        policy = ""
    if policy not in ("always", "on-failure", "on-abnormal", "on-abort"):
        return False, f"{reason} (and Restart={policy or 'unknown'}, so exiting would stay down)"
    log.warning("exiting non-zero so systemd (Restart=%s) restarts us", policy)
    # Runs on a worker thread, so this sleep costs the loop nothing
    # and gives the frames already sent time to reach the wire.
    time.sleep(1)
    os._exit(70)


async def restart_if_self_updated(link, kind: str, payload: dict) -> None:
    """Called after each finished task, once its answer is already
    posted.

    It does NOT wait for the queue to drain.  It used to, and on a
    busy agent that meant never: every held restart logged
    "holding the restart" while more work arrived, so a shipped fix
    could sit un-run for hours.  `spool_pending_work` writes the
    in-flight task AND the whole queue to disk on shutdown and the
    next process replays them, so restarting mid-queue costs a short
    delay rather than a lost message."""
    global _restart_pending, _restart_scheduled
    if _restart_scheduled:
        return

    if _restart_pending is None:
        if not self_updated():
            return
        room = payload.get("room") or {}
        _restart_pending = {
            "room_id":  room.get("hashid") if kind == "room" else None,
            "reply_to": (payload.get("message") or {}).get("hashid") if kind == "room" else None,
        }
        log.info("source on disk no longer matches this process — restart pending")

    queued = _work_queue.qsize() if _work_queue is not None else 0
    if queued:
        log.info("restarting with %d task(s) queued — the spool carries them over", queued)

    room_id    = _restart_pending.get("room_id")
    reply_to   = _restart_pending.get("reply_to")
    to_version = disk_agent_version() or "a newer build"
    sha        = head_sha(_SELF_FILES[0].parent)[:7]
    stamp      = f"{to_version}{f' ({sha})' if sha else ''}"

    ok, detail = await asyncio.to_thread(self_compiles)
    if not ok:
        _restart_scheduled = True  # don't retry a broken build every task
        log.error("refusing to restart into a build that will not compile: %s", detail)
        if room_id:
            await room_reply(link, room_id,
                             f"⚠️ I updated myself to {stamp} but it doesn't compile, so I'm "
                             f"staying on {AGENT_VERSION}. First error:\n\n"
                             f"```\n{_one_line(detail, 400)}\n```", reply_to)
        return

    # No room means nobody asked and nobody is waiting — restart, but
    # don't leave a notice for the next process to announce into a
    # room it would have to guess at.
    if room_id:
        await room_reply(link, room_id,
                         f"🔄 I updated myself to {stamp} — restarting, back in a few seconds.",
                         reply_to)
        write_restart_notice(room_id, reply_to, to_version)

    _restart_scheduled = True
    ok, detail = await asyncio.to_thread(schedule_restart)
    if ok:
        log.info("restart scheduled in %ss (%s)", RESTART_DELAY_SECONDS, detail)
        return

    log.error("could not schedule a restart: %s", detail)
    with contextlib.suppress(OSError):
        RESTART_NOTICE_PATH.unlink()
    if room_id:
        await room_reply(link, room_id,
                         f"⚠️ I updated myself to {stamp} but couldn't restart "
                         f"({detail}). I'm still answering from {AGENT_VERSION} — "
                         f"`sudo systemctl restart {SERVICE_UNIT}` when you get a chance.",
                         reply_to)


# ── Surviving a stop ──────────────────────────────────────────────
# systemd stops a unit by killing its whole cgroup.  Whatever the
# worker was running dies with it, and whatever was still queued dies
# unread — the server broadcast it once and does not repeat itself.
_current_work: tuple[str, dict] | None = None


def apply_queued_edit(room_hashid: str, message_hashid: str, body: str) -> bool:
    """Rewrite a queued room task whose message was edited.

    Only touches items still on the queue: once a task is in flight
    Claude is already reading the old words and there is nothing to
    swap. Rebuilt in order — asyncio.Queue has no way to mutate an
    item in place, and losing the ordering would reorder someone's
    conversation."""
    if _work_queue is None or not message_hashid:
        return False

    items: list[tuple[str, dict]] = []
    while not _work_queue.empty():
        try:
            items.append(_work_queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    changed = False
    for kind, payload in items:
        if kind != "room":
            continue
        msg = payload.get("message") or {}
        if msg.get("hashid") != message_hashid:
            continue
        if room_hashid and (payload.get("room") or {}).get("hashid") != room_hashid:
            continue
        msg["body"] = body
        payload["message"] = msg
        changed = True

    for item in items:
        _work_queue.put_nowait(item)
    return changed


def spool_pending_work() -> int:
    """Write the in-flight task and everything still queued to disk.

    The in-flight one is included deliberately: it was taken off the
    queue but never answered, so from the asker's side it is exactly
    as lost as the ones behind it."""
    items: list[dict] = []
    if _current_work:
        items.append({"kind": _current_work[0], "payload": _current_work[1]})
    if _work_queue is not None:
        while not _work_queue.empty():
            try:
                kind, payload = _work_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            items.append({"kind": kind, "payload": payload})

    if not items:
        with contextlib.suppress(OSError):
            WORK_SPOOL_PATH.unlink()
        return 0

    items = items[:WORK_SPOOL_MAX]
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        WORK_SPOOL_PATH.write_text(
            json.dumps({"at": time.time(), "items": items}), encoding="utf-8")
    except OSError as e:
        log.error("could not spool %d unfinished task(s): %s", len(items), e)
        return 0
    log.warning("spooled %d unfinished task(s) for the next process", len(items))
    return len(items)


def take_spooled_work() -> list[dict]:
    """Read and DELETE.  A spool that survived one boot would replay
    on every boot after it."""
    try:
        raw = WORK_SPOOL_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    with contextlib.suppress(OSError):
        WORK_SPOOL_PATH.unlink()
    try:
        spool = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(spool, dict):
        return []

    age = time.time() - float(spool.get("at") or 0)
    if age > WORK_SPOOL_MAX_AGE_SECONDS:
        log.warning("dropping a work spool %.0fs old — too late to be useful", age)
        return []
    return [item for item in spool.get("items") or []
            if isinstance(item, dict) and isinstance(item.get("payload"), dict)
            and item.get("kind")]


def install_shutdown_handler(loop) -> None:
    """SIGTERM is what `systemctl restart` sends.  Spool first, then
    go — the alternative is what happened on 2026-09-06: a restart
    armed while a request was queued, and the request evaporated."""
    def handle(signum):
        log.warning("received %s — spooling and shutting down", signal.Signals(signum).name)
        spool_pending_work()
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, handle, sig)


async def announce_restart(link) -> None:
    """The other half: the process that came back says so."""
    notice = take_restart_notice()
    if not notice:
        return
    was = notice.get("from_version") or "an earlier build"
    sha = head_sha(_SELF_FILES[0].parent)[:7]
    now = f"{AGENT_VERSION}{f' ({sha})' if sha else ''}"
    await room_reply(link, notice["room_id"],
                     f"✅ Back up on {now} — was {was}.", notice.get("reply_to"))


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
    summary  = proposal.get("summary") or "code proposal"

    # The SERVER resolved the workspace/project ship policy and put
    # the decision here.  Obey it; the proposal's own `mode` is only
    # the fallback for a server that predates the policy.
    policy = payload.get("policy") or {}
    mode   = policy.get("mode")
    if mode == "none":
        await reply(ws, chat_id, f"⚠️ Dispatch is disabled for this project — {policy.get('reason')}")
        return
    if mode not in ("ship", "pr"):
        mode = "pr" if proposal.get("mode") == "pull_request" else "ship"
    log.info("Approve: mode=%s reason=%s", mode, policy.get("reason"))
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

    # A proposal's `files` are the COMPLETE contents of each file as of
    # the commit it was generated against.  Writing them onto a branch
    # that has moved since silently reverts everything that landed in
    # between — so refuse, and say what to do about it.
    base = proposal.get("base_sha")
    if base:
        current = await asyncio.to_thread(head_sha, project_dir)
        if current and current != base:
            log.warning("stale proposal: generated on %s, HEAD is now %s", base[:12], current[:12])
            await reply(
                ws, chat_id,
                f"⚠️ This proposal was written against `{base[:12]}` but the branch is now "
                f"`{current[:12]}`. Its files are a snapshot of the older tree, so applying "
                f"it would undo whatever landed since. File the request again and I'll "
                f"rebuild it against current code.")
            return

    # Resolve EVERY path before writing ANY of them, so a proposal
    # with one bad entry is refused whole rather than half-applied.
    try:
        writes = []
        for f in files:
            path, content = f.get("path"), f.get("content")
            if not path or content is None:
                continue
            writes.append((safe_target(project_dir, path), content))
    except ValueError as e:
        log.error("refusing proposal: %s", e)
        await reply(ws, chat_id, f"⚠️ Refused — {e}. Nothing was written.")
        return

    try:
        for target, content in writes:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        if mode == "pr":
            outcome, pr = await asyncio.to_thread(
                _git_open_pr, project_dir, summary, files, policy,
                payload.get("feedback_id"))
            if pr:
                await reply(ws, chat_id, outcome, kind="pull_request", pull_request=pr)
            else:
                await reply(ws, chat_id, outcome)
        else:
            outcome = await asyncio.to_thread(_git_inline_ship, project_dir, summary,
                                              files, policy)
            await reply(ws, chat_id, outcome)
    except Exception as e:
        log.exception("apply failed")
        await reply(ws, chat_id, f"⚠️ Apply failed: {type(e).__name__}: {e}")


def safe_target(project_dir: Path, path: str) -> Path:
    """Resolve a proposal path INSIDE the project, or refuse.

    `project_dir / path` is not a containment check.  An absolute path
    REPLACES the base entirely (`Path("/a/b") / "/etc/x"` is
    `/etc/x`), and `../` walks out of it — so a proposal naming
    `~/.ssh/authorized_keys` or `~/.claude/settings.json` would be
    written there, as this user, by the apply step.  The model is told
    to send relative paths; that is an instruction, not a boundary.

    `.git/` is refused separately even though it IS inside the
    project: a proposal that writes `.git/hooks/pre-commit` executes
    on the very commit the caller is about to make."""
    if not path or "\x00" in path:
        raise ValueError("empty path")
    candidate = Path(path)
    if candidate.is_absolute():
        raise ValueError(f"{path!r} is absolute — proposal paths are relative to the project")

    root   = project_dir.resolve()
    target = (root / candidate).resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"{path!r} resolves outside the project ({target})")
    if target.relative_to(root).parts[0] == ".git":
        raise ValueError(f"{path!r} writes into .git/ — a hook there would run on the next commit")
    return target


def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    """Small subprocess helper — returncode, stdout, stderr."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=SUBPROCESS_HARD_CAP_SECONDS)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _git_inline_ship(project_dir: Path, summary: str, files: list[dict],
                     policy: dict | None = None) -> str:
    """Small-change path: commit the proposal's files onto the base
    branch and push, which auto-deploys."""
    policy = policy or {}
    base   = policy.get("base_ref") or _current_branch(project_dir)
    paths  = [ f["path"] for f in files if f.get("path") ]
    log.info("Inline ship onto %s: %s -> %s", base, summary, paths)

    on_base, err = _ensure_on_base(project_dir, base)
    if not on_base:
        return f"\u26a0\ufe0f Couldn't switch to `{base}`: {err[:200]}"

    _run(["git", "add", "--"] + paths, project_dir)

    # version_bump.sh rewrites the version constants and the
    # changelog; stage exactly those two, and only if it succeeded.
    vb = project_dir / "version_bump.sh"
    if vb.is_file():
        rc, _, err = _run(["bash", "./version_bump.sh"], project_dir)
        if rc == 0:
            _run(["git", "add", "--", "config/application.rb", "CHANGELOG.md"], project_dir)
        else:
            log.warning("version_bump.sh failed: %s", err[:200])

    msg = f"UI-feedback tweak: {summary}"
    rc, _, err = _run(["git", "commit", "-m", msg], project_dir)
    if rc != 0:
        return f"\u26a0\ufe0f Commit failed: {err[:300]}"

    rc, out, err = _run(["git", "rev-parse", "HEAD"], project_dir)
    sha = (out or "").strip()[:12]

    rc, _, err = _run(["git", "push", "origin", base], project_dir)
    if rc != 0:
        return f"\u26a0\ufe0f Push failed after commit {sha}: {err[:300]}"

    reason = policy.get("reason")
    suffix = f" ({reason})" if reason else ""
    return f"\u2705 Shipped `{sha}` to `{base}` \u2014 {summary}.{suffix} Auto-deploy is rolling."


def _git_open_pr(project_dir: Path, summary: str, files: list[dict],
                 policy: dict | None = None,
                 feedback_id: str | None = None) -> tuple[str, dict | None]:
    """Bigger-change path: branch off the base ref, commit, push, and
    open a PR for a human to merge.

    Returns (message, pull_request | None).  The checkout is ALWAYS
    returned to the base branch, including on failure — leaving it on
    the feature branch meant the next inline ship silently committed
    onto someone else's PR."""
    policy = policy or {}
    base   = policy.get("base_ref") or _current_branch(project_dir)
    prefix = policy.get("branch_prefix") or "feedback/"
    paths  = [ f["path"] for f in files if f.get("path") ]

    slug = re.sub(r"[^a-z0-9-]+", "-", summary.lower())[:40].strip("-") or "ui"
    branch = f"{prefix}{slug}-{uuid.uuid4().hex[:6]}"

    log.info("Open PR on %s (base %s): %s", branch, base, summary)

    _run(["git", "fetch", "origin", base], project_dir)
    on_base, err = _ensure_on_base(project_dir, base)
    if not on_base:
        return f"\u26a0\ufe0f Couldn't switch to `{base}`: {err[:200]}", None

    try:
        rc, _, err = _run(["git", "checkout", "-b", branch], project_dir)
        if rc != 0:
            return f"\u26a0\ufe0f Branch failed: {err[:300]}", None

        _run(["git", "add", "--"] + paths, project_dir)
        rc, _, err = _run(["git", "commit", "-m", f"UI feedback: {summary}"], project_dir)
        if rc != 0:
            return f"\u26a0\ufe0f Commit failed: {err[:300]}", None

        rc, _, err = _run(["git", "push", "-u", "origin", branch], project_dir)
        if rc != 0:
            return f"\u26a0\ufe0f Push failed: {err[:300]}", None

        body = "Filed via vroxy_dispatch from an admin UI-feedback note."
        if policy.get("reason"):
            body += f"\n\nRouted to a PR because: {policy['reason']}."
        if feedback_id:
            body += f"\n\nFeedback: `{feedback_id}`"

        argv = ["gh", "pr", "create", "--base", base, "--head", branch,
                "--title", f"UI feedback: {summary}", "--body", body]
        if policy.get("pr_draft"):
            argv.append("--draft")

        rc, out, err = _run(argv, project_dir)
        if rc != 0:
            return f"\u26a0\ufe0f `gh pr create` failed: {err[:300]}", None

        url = (out or "").strip().splitlines()[-1] if out.strip() else ""
        number = None
        match = re.search(r"/pull/(\d+)", url)
        if match:
            number = int(match.group(1))

        reason = policy.get("reason")
        suffix = f" ({reason})" if reason else ""
        pr = {"url": url, "number": number, "branch": branch, "base": base}
        return f"\u2705 PR opened for review — {summary}.{suffix} {url}", pr
    finally:
        # Never leave the operator's checkout on a feature branch.
        back, err = _ensure_on_base(project_dir, base)
        if not back:
            log.error("could not return to %s after PR: %s", base, err[:200])


def _current_branch(project_dir: Path) -> str:
    rc, out, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], project_dir)
    return (out or "").strip() or "main"


def _ensure_on_base(project_dir: Path, base: str) -> tuple[bool, str]:
    """Checkout `base` unless we're already there.  Refuses rather
    than forcing when the tree is dirty \u2014 an operator's uncommitted
    work is never dispatch's to discard."""
    if _current_branch(project_dir) == base:
        return True, ""
    rc, out, _ = _run(["git", "status", "--porcelain"], project_dir)
    if (out or "").strip():
        return False, "the working tree has uncommitted changes"
    rc, _, err = _run(["git", "checkout", base], project_dir)
    return rc == 0, err


ROOM_PROGRESS_MAX = 1000


def progress_action(sent: int, truncated: bool) -> str:
    if sent < ROOM_PROGRESS_MAX:
        return "send"
    return "drop" if truncated else "notice"
ROOM_RUN_STEPS_MAX = 500
ROOM_PROGRESS_TEXT_MAX = 400


def _progress_line(name: str, input_dict: dict | None) -> str:
    """One glanceable line: the tool and the argument that says what
    it touched.  A whole input dict on a chat line is unreadable, and
    the interesting part is almost always the path or the command."""
    interesting = ("file_path", "path", "command", "pattern", "query",
                   "url", "prompt", "description")
    detail = ""
    if isinstance(input_dict, dict):
        for key in interesting:
            value = input_dict.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip().splitlines()[0][:120]
                break
    return f"{name}({detail})" if detail else str(name)


def _stall_line(event: dict) -> str:
    """What the room sees when a run goes quiet.  Silence is the one
    thing a progress trail must never render as nothing."""
    seconds = int(event.get("seconds") or STALL_SECONDS)
    window  = int(event.get("window") or 1)
    limit   = int(event.get("max_windows") or STALL_WINDOWS_BEFORE_KILL)
    if window >= limit:
        return f"no output for {seconds}s — giving up on this run"
    return f"still working — nothing back for {seconds}s"


async def _emit_room_run(ws, room_id: str, reply_to: str | None,
                         steps: list[dict], result: dict) -> None:
    """One `room_run` action at the end of a run — Rails writes a
    DispatchRun row holding the working log, the token counts and the
    CLI's own cost figure.  Sent once rather than per step: a long run
    makes hundreds of steps and they're read as one document.

    Best-effort.  The answer is already in the room by the time this
    goes out, and bookkeeping must never be what breaks a reply."""
    try:
        usage = result.get("usage") or {}
        await cable_send(ws, "message", {
            "action":        "room_run",
            "room_id":       room_id,
            "reply_to":      reply_to,
            "status":        "error" if result.get("is_error") else "ok",
            "agent_version": AGENT_VERSION,
            "project":       PROJECT,
            "usage":         usage,
            "cost_usd":      result.get("cost_usd"),
            "duration_ms":   result.get("duration_ms"),
            "num_turns":     result.get("num_turns"),
            "steps":         steps[:ROOM_RUN_STEPS_MAX],
        })
    except Exception:
        log.exception("emit_room_run failed")


async def _emit_room_progress(ws, room_id: str, reply_to: str | None,
                              kind: str, text: str) -> None:
    """One `room_progress` action — Rails broadcasts it on the room's
    channel and keeps it in Redis for three days, so a refresh finds
    the trail but nothing lands in the database.  Non-fatal on error:
    a progress line is never worth losing the answer over."""
    try:
        await cable_send(ws, "message", {
            "action":   "room_progress",
            "room_id":  room_id,
            "reply_to": reply_to,
            "kind":     kind,
            "text":     text,
        })
    except Exception:
        log.exception("emit_room_progress failed")


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
    set_task_label(f"feedback {fb.get('hashid') or ''} {fb.get('note') or ''}")
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

    project_dir = CODE_ROOT / PROJECT
    if not project_dir.is_dir():
        await reply(ws, chat_id, f"⚠️ Can't investigate: {project_dir} not found.")
        return

    # The proposal phase runs in a throwaway worktree, so an
    # investigation that edits files leaves the real checkout alone.
    try:
        with proposal_worktree(project_dir) as worktree:
            raw, stats, changed = await _run_proposal(ws, chat_id, prompt,
                                                      worktree, on_stream_event)
    except subprocess.TimeoutExpired:
        await reply(ws, chat_id, "⚠️ Claude timed out after 30 minutes.")
        return
    except Exception as e:
        log.exception("claude crashed")
        await reply(ws, chat_id, f"⚠️ Claude crashed: {type(e).__name__}: {e}")
        return

    if not raw and not changed:
        await reply(ws, chat_id, "(Claude returned an empty response.)")
        return

    body, proposal = parse_proposal(raw or "")

    # Claude edited the tree but never emitted a fenced proposal.
    # The work is real and on disk; salvage it rather than dropping it.
    if proposal is None and changed:
        log.info("no fenced proposal but %d file(s) changed — building one", len(changed))
        proposal = {"mode": "inline_ship",
                    "summary": _one_line(body, 80) or "dispatch change",
                    "files": changed}

    if proposal:
        if stats:
            proposal["stats"] = stats
        base = await asyncio.to_thread(head_sha, project_dir)
        if base:
            proposal["base_sha"] = base
        await reply(ws, chat_id, body or proposal.get("summary", ""),
                    kind="code_proposal", proposal=proposal)
    else:
        await reply(ws, chat_id, body)


async def _run_proposal(ws, chat_id, prompt, worktree, on_stream_event):
    """Run Claude inside the worktree and report back what it did:
    (raw text, diffstat, changed files as proposal entries)."""
    if STREAM_ENABLED:
        try:
            raw = await asyncio.to_thread(
                run_agent_streamed, prompt, PROJECT, on_stream_event,
                True, None, worktree)
        except FileNotFoundError:
            raise
        except Exception:
            log.exception("streamed run failed — falling back to non-streamed")
            raw = await asyncio.to_thread(run_claude, prompt, PROJECT, worktree)
    else:
        raw = await asyncio.to_thread(run_claude, prompt, PROJECT, worktree)

    stats   = await asyncio.to_thread(worktree_diffstat, worktree)
    changed = await asyncio.to_thread(worktree_changed_files, worktree)
    if stats:
        log.info("proposal diffstat: %s file(s) +%s -%s",
                 stats.get("files"), stats.get("insertions"), stats.get("deletions"))
    return raw, stats, changed


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


# ── Clickable questions ───────────────────────────────────────────
ASK_FENCE_RE    = re.compile(r"```ask\s*(\{.*?\})\s*```", re.DOTALL)
ASK_MODES       = ("one", "many", "text")
ASK_MAX_OPTIONS = 12
ASK_LABEL_MAX   = 120
ASK_PROMPT_MAX  = 500
ASK_JSON_MAX    = 20_000
ASK_FALLBACK_HINT = "Reply with a number or the option, whichever is easier."


def _flatten(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _ask_options(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    options: list[dict] = []
    seen: set[str] = set()
    for opt in raw:
        if isinstance(opt, dict):
            label, value = opt.get("label"), opt.get("value")
        else:
            label = value = opt
        if isinstance(label, (dict, list)) or isinstance(value, (dict, list)):
            continue
        label = str("" if label is None else label).strip()[:ASK_LABEL_MAX]
        if not label:
            continue
        value = str("" if value is None else value).strip()[:ASK_LABEL_MAX] or label
        if value in seen:
            continue
        seen.add(value)
        options.append({"label": label, "value": value})
        if len(options) == ASK_MAX_OPTIONS:
            break
    return options


def _parse_room_ask(raw: str) -> tuple[str, dict | None]:
    match = ASK_FENCE_RE.search(raw or "")
    if not match:
        return raw, None

    blob = match.group(1)
    if len(blob) > ASK_JSON_MAX:
        log.warning("Ask block is %d chars — leaving it as text", len(blob))
        return raw, None
    try:
        declared = json.loads(blob)
    except ValueError as e:
        log.warning("Ask JSON invalid: %s — leaving it as text", e)
        return raw, None
    if not isinstance(declared, dict):
        return raw, None

    prompt  = str(declared.get("prompt") or "").strip()[:ASK_PROMPT_MAX]
    mode    = str(declared.get("mode") or "one").strip().lower()
    mode    = mode if mode in ASK_MODES else "one"
    options = _ask_options(declared.get("options"))
    if not prompt or (not options and mode != "text"):
        log.warning("Ask block unusable (mode=%s, %d options) — leaving it as text",
                    mode, len(options))
        return raw, None

    return ASK_FENCE_RE.sub("", raw).strip(), {
        "prompt": prompt, "mode": mode, "options": options,
    }


def parse_room_ask(raw: str) -> tuple[str, dict | None]:
    """Splits Claude's room answer into (body, ask_or_None), with the
    fence stripped out of the body.  A block this can't turn into a
    renderable question leaves the raw text exactly as it came."""
    try:
        return _parse_room_ask(raw)
    except Exception as e:
        log.warning("Ask parse failed (%s: %s) — leaving it as text", type(e).__name__, e)
        return raw, None


def ask_fallback(body: str, ask: dict) -> str:
    """The question restated in prose, so a surface that renders no
    buttons is still answerable by typing.  Appends only what the
    model's own prose left out."""
    body = (body or "").strip()
    said = _flatten(body)
    blocks: list[str] = []

    if _flatten(ask["prompt"]) not in said:
        blocks.append(ask["prompt"])
    if any(_flatten(o["label"]) not in said for o in ask["options"]):
        blocks.append("\n".join(f"{i}. {o['label']}"
                                for i, o in enumerate(ask["options"], 1)))
        blocks.append(ASK_FALLBACK_HINT)

    if not blocks:
        return body
    tail = "\n\n".join(blocks)
    return f"{body}\n\n{tail}" if body else tail



# Which DispatchAgent kind this process answers as.  The server keys
# an agent row on the engine it reports, so the two must agree or a
# codex instance registers itself as Claude Code.
ENGINE_AGENT_KINDS = {"claude": "claude_code", "codex": "codex"}


def _is_ours(payload: dict) -> bool:
    """Whether work addressed to an agent belongs to THIS instance.

    Local agents all subscribe to the same tenant channel, so a
    workspace running one Claude instance and one Codex instance
    sees every room message twice.  The server names the agent it
    routed to; anything addressed to another engine is somebody
    else's turn.

    A frame with no agent block predates this and is answered as
    before — an older server must not go silent against a newer
    dispatch.
    """
    agent = payload.get("agent")
    if not isinstance(agent, dict):
        return True
    kind = (agent.get("kind") or "").strip()
    if not kind:
        return True
    return kind == ENGINE_AGENT_KINDS.get(DISPATCH_ENGINE, "claude_code")


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

    if not _is_ours(payload):
        agent = payload.get("agent") or {}
        log.info("room.message for %s (%s) — not this engine (%s), leaving it",
                 agent.get("name"), agent.get("kind"), DISPATCH_ENGINE)
        return

    body = (msg.get("body") or "").strip()
    set_task_label(f"#{room.get('name') or room_id} {body}")
    log.info("Handling room message room=%s (#%s) from=%s body=%s",
             room_id, room.get("name"), sender.get("name"),
             _one_line(body, ROOM_LOG_BODY_MAX))

    command = room_command(body)
    if command:
        removed = clear_sessions([_room_session_key(room_id)])
        await room_reply(ws, room_id,
                         "🧹 Fresh session — I've forgotten this room's history."
                         if removed else
                         "🧹 Already on a fresh session (nothing to clear).")
        return

    # Fetched before the prompt is built so the paths can go in it,
    # and off the event loop because it's blocking network I/O.
    attachments = await asyncio.to_thread(download_attachments, msg)
    prompt = build_room_prompt(payload, attachments)
    loop = asyncio.get_running_loop()
    sent = 0
    truncated = False
    # Kept alongside the live sends so the finished run can be saved
    # as one document, with the tokens and cost the CLI reports.
    steps: list[dict] = []
    result: dict = {}

    def send_line(kind: str, text: str) -> None:
        fut = asyncio.run_coroutine_threadsafe(
            _emit_room_progress(ws, room_id, msg.get("hashid"), kind, text), loop)
        fut.add_done_callback(_log_future_error)

    def emit(kind: str, text: str) -> None:
        # Runs on the claude thread, so hop back to the loop to send.
        nonlocal sent, truncated
        if not text:
            return
        if len(steps) < ROOM_RUN_STEPS_MAX:
            steps.append({"kind": kind, "text": text})
        action = progress_action(sent, truncated)
        if action == "drop":
            return
        if action == "notice":
            truncated = True
            send_line("text", f"Working log truncated after {ROOM_PROGRESS_MAX} lines "
                              "— the run is still going; tail the dispatch log for the rest.")
            return
        sent += 1
        send_line(kind, text)

    trail = ProgressTrail(emit, ROOM_PROGRESS_TEXT_MAX)

    def on_stream_event(event: dict) -> None:
        if event.get("type") == "result":
            result.update(event)
        trail.feed(event)

    try:
        raw = await asyncio.to_thread(
            run_agent_streamed, prompt, PROJECT, on_stream_event,
            True, _room_session_key(room_id))
    except subprocess.TimeoutExpired:
        await room_reply(ws, room_id, "⚠️ I timed out after 30 minutes.", msg.get("hashid"))
        result["is_error"] = True
        await _emit_room_run(ws, room_id, msg.get("hashid"), steps, result)
        return
    except Exception as e:
        log.exception("claude crashed on room message")
        await room_reply(ws, room_id, f"⚠️ I crashed: {type(e).__name__}: {e}", msg.get("hashid"))
        result["is_error"] = True
        await _emit_room_run(ws, room_id, msg.get("hashid"), steps, result)
        return
    if not raw:
        await room_reply(ws, room_id, "(I came back with an empty response.)", msg.get("hashid"))
        result["is_error"] = True
        await _emit_room_run(ws, room_id, msg.get("hashid"), steps, result)
        return

    body_text, ask = parse_room_ask(raw)
    if ask:
        body_text = ask_fallback(body_text, ask)

    # Threaded under the message that asked.  A room where three
    # people are talking at once is unreadable when the answers float
    # free of their questions.
    posted = await post_room_reply(ws, room_id, split_room_body(body_text),
                                   msg.get("hashid"))

    if ask:
        try:
            await room_ask(ws, room_id, posted, ask)
        except Exception:
            log.exception("could not send the ask — the reply is already posted")

    # After the answer: the run is only worth recording once the
    # person has it, and a failure here must not look like a failure
    # to reply.
    await _emit_room_run(ws, room_id, msg.get("hashid"), steps, result)


# The work queue and its worker live for the PROCESS, not for one
# connection.  They used to be created inside process_stream and
# cancelled in its `finally`, so every reconnect destroyed the run in
# flight — the Claude thread kept going, finished, and had nothing
# left to reply through.
_work_queue: asyncio.Queue | None = None


async def worker_loop(link: CableLink) -> None:
    """One in-flight handler at a time — Claude sessions aren't
    reentrant and we don't want to race a `--resume` with itself."""
    global _current_status
    assert _work_queue is not None
    global _current_work
    while True:
        kind, payload = await _work_queue.get()
        _current_work = (kind, payload)
        fb_id = ((payload.get("feedback") or {}).get("hashid")
                 or payload.get("feedback_id") or "?")
        if kind == "room":
            room = payload.get("room") or {}
            _current_status = f"answering #{room.get('name') or room.get('hashid')}"
            with contextlib.suppress(Exception):
                await room_status(link, room.get("hashid"),
                                  (payload.get("message") or {}).get("hashid"), "working")
        else:
            _current_status = f"processing feedback {fb_id}" if kind == "feedback" \
                              else f"applying feedback {fb_id}"
        try:
            if kind == "feedback":
                await handle_feedback(link, payload)
            elif kind == "approve":
                await handle_approve(link, payload)
            elif kind == "room":
                await handle_room_message(link, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker failed on payload=%s", payload)
        finally:
            _current_status = "idle"
            _current_work = None
            set_task_label("")
            _work_queue.task_done()

        # Outside the try so a task's own failure doesn't mask it,
        # and guarded so a bug in here can't take the worker down and
        # leave dispatch connected but deaf.
        try:
            await restart_if_self_updated(link, kind, payload)
        except Exception:
            log.exception("self-update check failed")


async def process_stream(link: CableLink, ws) -> None:
    """Read one connection's frames onto the shared work queue.

    Returns when the socket closes.  It deliberately owns NOTHING that
    outlives the connection — the worker and the queue are the
    process's, so a reconnect is invisible to a run in progress."""
    assert _work_queue is not None
    link.attach(ws)
    heartbeat_task = asyncio.create_task(heartbeat_forever(link))

    try:
        async for raw in ws:
            frame = json.loads(raw)
            frame_type = frame.get("type")

            if frame_type == "welcome":
                log.info("Cable connected — subscribing")
                await subscribe(link)
                continue
            if frame_type == "confirm_subscription":
                log.info("Subscribed to AdminFeedbackChannel")
                # Anything that finished while the cable was down goes
                # out now that the channel will accept it.
                await link.flush()
                try:
                    await announce_restart(link)
                except Exception:
                    log.exception("could not announce the restart")
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
                await _work_queue.put(("feedback", msg))
            elif msg.get("type") == "feedback.followup":
                await _work_queue.put(("feedback", msg))
            elif msg.get("type") == "approve.requested":
                await _work_queue.put(("approve", msg))
            elif msg.get("type") == "room.message":
                await _work_queue.put(("room", msg))
                with contextlib.suppress(Exception):
                    await room_status(link, (msg.get("room") or {}).get("hashid"),
                                      (msg.get("message") or {}).get("hashid"), "queued")
            elif msg.get("type") == "room.message.edited":
                edited = msg.get("message") or {}
                if apply_queued_edit((msg.get("room") or {}).get("hashid"),
                                     edited.get("hashid"), edited.get("body") or ""):
                    log.info("queued task updated after an edit message=%s",
                             edited.get("hashid"))
            elif msg.get("type") == "room_reply.posted":
                note_posted_message(msg.get("room_id"), msg.get("message_id"))
            else:
                log.debug("Ignoring message type=%s", msg.get("type"))
    finally:
        heartbeat_task.cancel()
        link.detach()


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

    global _work_queue
    _work_queue = asyncio.Queue()
    install_shutdown_handler(asyncio.get_running_loop())

    # Anything the previous process was holding when it was stopped.
    # Re-queued before the socket is even open, so it runs in the
    # order it was asked rather than behind whatever arrives next.
    for item in take_spooled_work():
        log.warning("replaying a %s task the last process never finished", item["kind"])
        _work_queue.put_nowait((item["kind"], item["payload"]))

    link = CableLink()
    # Started once, outside the reconnect loop, so a run in progress
    # survives the cable dropping under it.
    worker_task = asyncio.create_task(worker_loop(link))

    backoff = 1
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30, origin=origin) as ws:
                backoff = 1
                await process_stream(link, ws)
        # WebSocketException covers a refused handshake as well as a
        # dropped socket.  A deploy answers `502` for a few seconds
        # while the new container boots, and that used to fall through
        # to the catch-all below: logged as an unexpected error with a
        # full traceback, and retried on a flat 5s instead of the
        # backoff.  A rejected handshake during a deploy is the most
        # ordinary thing that happens to this process.
        except (websockets.WebSocketException, OSError) as e:
            log.warning("Cable unavailable (%s: %s) — reconnecting in %ss",
                        type(e).__name__, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception:
            log.exception("Unexpected error in cable loop")
            await asyncio.sleep(5)
        finally:
            if worker_task.done():
                # The worker dying silently would leave dispatch
                # connected and permanently deaf.
                log.error("worker stopped unexpectedly — restarting it")
                worker_task = asyncio.create_task(worker_loop(link))


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
