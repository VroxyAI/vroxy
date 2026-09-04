# Changelog

## 0.5.0

- **A deploy no longer throws away the run it interrupted.** The work
  queue and its worker were created inside `process_stream` and
  cancelled in its `finally`, so any reconnect destroyed the run in
  flight — the Claude thread kept going, finished, spent its tokens,
  and had nothing left to reply through. Seen for real: a 56-tool-call
  run completed with 3,275 characters of answer at 13:24:06 and no
  reply was ever sent. The queue and worker now live for the process.
- **Handlers hold a `CableLink`, not a socket.** It swaps the
  underlying connection on reconnect and buffers anything sent while
  there isn't one, so a result produced during a deploy is delivered
  when the server comes back rather than written into a dead socket.
  The outbox is bounded and drops oldest-first — a fresh reply
  outranks a stale progress chip.
- Heartbeats and subscribes are explicitly *not* buffered: replaying a
  heartbeat after a reconnect reports a stale status as current, and a
  subscribe belongs to the socket that asked for it.
- The worker is restarted if it ever stops, since a dead worker left
  dispatch connected and permanently deaf.

## 0.4.0

- **Security: a proposal could write outside the project.** The apply
  step did `project_dir / path` with a path the model wrote, and that
  is not a containment check — an absolute path REPLACES the base
  (`Path("/a/b") / "/etc/x"` is `/etc/x`) and `../` walks out of it.
  A proposal naming `~/.ssh/authorized_keys` or
  `~/.claude/settings.json` would have been written there, as this
  user, by the Apply button. Paths are now resolved and confined to
  the project.
- **`.git/` is refused too**, even though it is inside the project: a
  proposal writing `.git/hooks/pre-commit` executes on the very commit
  the apply step is about to make.
- Every path is resolved BEFORE any file is written, so a proposal
  with one bad entry is refused whole rather than half-applied.
- `git add --` so a path can never be parsed as a git flag.

## 0.3.1

- **Refuse a stale proposal instead of reverting work with it.** A
  proposal carries the COMPLETE contents of each file as of the commit
  it was generated against, so applying one after the branch has moved
  silently undoes everything that landed in between. Proposals now
  record `base_sha`, and `handle_approve` compares it to HEAD before
  writing anything — a mismatch explains the drift and asks for the
  request to be filed again rather than quietly rewinding the repo.

## 0.3.0

- **The proposal phase runs in a throwaway git worktree.** Claude
  investigates and edits freely in a detached checkout of HEAD, so
  nothing reaches the real working tree before you approve —
  "pending review" is now literally true. Previously an
  investigation could (and did) leave edits on disk under a card
  that claimed nothing had happened yet.
- The worktree is created as a SIBLING of the project inside
  `CODE_ROOT`, never in `/tmp`: that's what keeps the parent
  workspace's `CLAUDE.md` loading and sibling repos resolving at
  `../vroxy_dispatch`, so a dispatch run has the same context a
  human working in that folder does.
- Because the worktree starts clean, `git diff --numstat` gives an
  exact diffstat, which now rides along with the proposal as
  `stats` and is what the server's ship policy sizes on.
- A run that edits files but emits no fenced proposal has one built
  from the worktree diff rather than losing the work.
- **Dispatch no longer decides how a change ships.** The server
  resolves the workspace/project ship policy and sends the decision
  in `approve.requested`; `handle_approve` obeys it. The system
  prompt now tells the model how the workspace ships instead of
  asking it to infer "PR or not" from the wording of the note.
- **`_git_open_pr` rewritten** — it never returned to the base
  branch, so the next inline ship silently committed onto the
  previous PR's branch. It now branches from `origin/<base_ref>`
  after a fetch, passes `--base`, adds a random suffix so the same
  summary twice can't collide, honors `pr_draft`, links the PR body
  back to the feedback, and returns to the base branch in a
  `finally` — including when `gh pr create` fails.
- `_git_inline_ship` commits to the configured base branch rather
  than whatever happened to be checked out, and both paths refuse
  to switch branches over uncommitted work rather than discarding
  an operator's changes.
- A PR is reported back to the chat as a structured
  `kind: "pull_request"` reply so it renders as a link card.

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
