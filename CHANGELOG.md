# Changelog

## 0.2.1

- **Logs to a file as well as the terminal.** `log/dispatch.log`,
  rotating at 10 MB × 5 backups, so a run started in a shell stays
  readable (`tail -f`) after that shell is gone — and so a turn that
  misbehaved can be read back rather than reconstructed. `LOG_FILE`
  overrides the path, `LOG_FILE=""` disables it, and an unwritable
  path warns instead of taking dispatch down.

## 0.2.0

- **Rooms.** Dispatch now reads and answers in workspace chat rooms,
  not just the feedback queue. Handles `room.message`, replies with
  the new `room_reply` action (posted as the `dispatch@vroxy.ai` bot
  through `RoomMessageService`), and keeps a live typing indicator
  via `room_typing` for the length of a run. Per-room opt-in on the
  Rails side (`Room#dispatch_mode`: `off` / `mentioned` / `all`); the
  bot's own posts and unaddressed webhook pings never trigger a run.
- Room answers are conversation, not the feedback pipeline: a chat-
  shaped system prompt, no proposal block, no tool chips in the room
  log, and each room gets its own Claude session.
- Long answers split across messages at `RoomMessage::BODY_MAX` on
  paragraph boundaries rather than being truncated.
- **`--reset`** clears stored Claude sessions and exits: all of this
  project's by default, or named rooms by hashid. `--sessions` lists
  what's stored. The same thing is reachable in-room as `/reset`
  (aliases `/clear`, `/new`), which answers without a Claude run.
  `bin/claude-chat /clear` only ever cleared the non-streamed
  session, which is why it never fixed the streamed path.

## 0.1.1

- Recover from a stale `--resume` session id instead of reporting
  "(Claude returned an empty response.)" forever. A stored id can
  outlive its transcript (cleared history, different host, wiped
  `~/.claude`); `claude --resume` then exits 1 having produced
  nothing. Both the streamed path and `bin/claude-chat` now drop the
  id and retry fresh once on that signature.
- Stop persisting the session id when the run failed — the streamed
  path wrote the dead id back after every failure, so the state was
  self-perpetuating.
- A streamed run that exits non-zero with no text and no tool calls
  now raises (with stderr attached) rather than returning `""`, so
  the non-streamed fallback actually engages and the operator sees
  the real error.

## 0.1.0

- Initial port from `vroxy_dispatch/feedback_agent.py`.
- Adapted to ctovibe_web's tenant-scoped `AdminFeedbackChannel`:
  service auth via `Tenant`-owned `ApiToken` (`platform:dispatch` or
  `full` scope); one dispatch process = one tenant; hashid chat/
  message ids on the wire; heartbeat cached under
  `ctovibe_dispatch:heartbeat:<tenant.id>` with a 60 s TTL.
- Handles `feedback.created`, `feedback.followup`, `approve.requested`.
- Streamed Claude output forwarded as live tool chips via `progress`;
  falls back to `claude-chat` (non-streamed) if the stream flag
  breaks.
- Inline-ship path bumps `version_bump.sh` when present (matches
  ctovibe_web layout); PR path branches + `gh pr create`s.
- `bin/claude-chat` copied verbatim from vroxy_dispatch (project-
  agnostic).
- 8 unit tests (prompt build + proposal parse).
