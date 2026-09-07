# Changelog

## 0.17.0

### Fixed

- **The room stopped showing the working log after 120 lines.** `emit`
  returned early once `sent` hit `ROOM_PROGRESS_MAX`, so a longer run
  went silent in the web and mobile trail while the terminal log kept
  scrolling — indistinguishable from a wedged run. The live ceiling is
  now 1000, and hitting it posts one line saying the trail was
  truncated instead of just stopping. Everything downstream already
  trims oldest-first (300 in Redis, 300 in both clients), so the newest
  activity is what survives.
- The saved run document no longer inherits the live cap: `steps` fills
  to `ROOM_RUN_STEPS_MAX` (500) on its own, where before it stopped at
  120 alongside the emit and made that constant dead.

## 0.16.0

- **Nothing waits on a response for more than 90 seconds.** The
  streamed run had no timeout at all: iterating `proc.stdout` blocks
  with no way out, so a wedged claude — a hung tool call, a dead
  network read — held dispatch open forever behind a typing indicator
  and the asker never heard back. A daemon thread now does the
  blocking read and the run loop waits on a queue it can time out.
- Every window of silence is reported to the room ("still working —
  nothing back for 90s") instead of rendering as nothing.
  `STALL_WINDOWS_BEFORE_KILL` consecutive windows means wedged, not
  thinking: the process group is killed (SIGTERM, then SIGKILL) and
  whatever the run did produce is returned with a note. The claude
  subprocess gets its own session so the kill can't reach dispatch.
- The two blind-wait paths — the non-streamed fallback (was 1800 s)
  and the git helper (was 300 s) — now share one
  `SUBPROCESS_HARD_CAP_SECONDS`, and a test fails if any timeout
  literal in the module climbs back over it.
- **The room prompt teaches the ceiling.** Every Bash call gets
  `timeout: 90000` or less; run the tests that cover the change, never
  the whole suite; background anything genuinely long and poll it
  rather than raising the timeout. Whole-suite runs (`bin/system-test`
  with no argument at 144 s, bare `bash ./test.sh`) were the single
  biggest chunk of a room turn's wall clock.
- Room messages log 1000 characters instead of 80 — reconstructing
  what was actually asked was impossible from an 80-character prefix.
- **The ask buttons are wired end to end.** `room_reply` echoes
  `room_reply.posted` with the hashid it just created, `post_room_reply`
  waits for each chunk's acknowledgement so the ask hangs off the
  message that *ends* the reply, and `room_ask` emits against it. A
  server that never acknowledges costs one timeout and the ask, never
  the reply.

## 0.15.0

- **Claude can ask the room a question instead of guessing.** A reply
  may end with a fenced ` ```ask ` block declaring a prompt, a mode
  (`one` / `many` / `text`) and its options; the room prompt teaches
  the shape with one example, which is what makes it fire at all.
- The fence never reaches the room. `parse_room_ask` strips it and
  `ask_fallback` appends only what the prose left out — the prompt if
  it wasn't restated, a numbered option list if the options weren't
  named. The widget and mobile builds that render no buttons still get
  a question someone can answer by typing "2".
- **The reply always survives the ask.** Unparseable JSON, a non-object,
  a block over 20 KB, a missing prompt, or no options in a mode that
  needs them all leave the raw text exactly as the model wrote it, and
  the whole parse is wrapped so nothing it does can end a run. Options
  normalize to `RoomAsk`'s own caps (12 options, 120-char labels,
  500-char prompt) so what the fallback lists is what the room stores.
- 19 tests for the two functions.
- **Buttons are not wired up yet.** `AdminFeedbackChannel#room_ask`
  needs the hashid of the message the question hangs under, and nothing
  reports the hashid of a reply dispatch just posted: `room_reply`
  returns nothing, `/api/v1` has no rooms endpoint, `RoomChannel`
  rejects a connection with no `current_user`, and the bot's own post
  never re-broadcasts. Emitting `room_ask` is a few lines once
  `room_reply` echoes the posted hashid back.

## 0.14.0

- **The narration between tool calls now reaches the room.** It was
  being dropped twice: `text_delta` returned early in the room
  handler, and whatever had accumulated was then overwritten in the
  reply by the CLI's own `result` text. So "Now the view marker, the
  copy-link action, and the JS" — the most readable line in a run —
  went nowhere, and the trail was tool calls only.
- The rule it turns on: a text block is NARRATION when more work
  follows it and the ANSWER when nothing does, and which it is can't
  be known until the next event arrives. So text is HELD — flushed as
  a trail line when a tool call or a thought comes next, dropped at
  `result`, because by then it is the reply and saying it twice helps
  nobody. Consecutive text with no work between it is one line, not
  two, so a multi-block answer isn't half-echoed into the trail.
- Extracted as `ProgressTrail` rather than left in the closure, so the
  rule is testable: 7 tests covering narration vs answer, consecutive
  blocks, a run ending on a tool call, a one-line answer producing no
  trail at all, and trimming.

## 0.13.0

- **`install.sh`** — installs, updates and removes dispatch instances.
  Creates the venv, installs a systemd TEMPLATE unit
  (`vroxy-dispatch@<workspace>.service`) so a second workspace costs
  one env file rather than a second copy of the unit, asks for the
  host and a workspace token, and **confirms the workspace by name**
  via `GET /api/v1/whoami` before wiring anything to it. `--update`
  pulls, reinstalls deps and restarts every instance from OUTSIDE
  their cgroups; `--list` and `--remove` do the obvious.
- The token is read with `read -s` and written to a 0640 file owned by
  root — never to stdout, the shell history, or the process list.
- **`VROXY_INSTALL_ID` / `VROXY_AGENT_NAME` ride on the heartbeat.**
  The server (vroxy_web 2.82.0) resolves the agent row by the install
  id and registers a new one the first time it sees it, which is what
  lets several dispatch processes serve one workspace — each with its
  own room and its own `@Name`. Only sent when configured: an install
  predating `install.sh` keeps the old single-agent resolution instead
  of registering a duplicate under a name nobody chose.
- The install id is generated once and never regenerated. Rewriting it
  would orphan the agent row, its room and its history.

## 0.12.0

- **Says where a request is before it produces anything.** New
  `room_status` action (vroxy_web 2.80.0): `queued` the moment a room
  message lands on the work queue, `working` when the worker picks it
  up. One in-flight run at a time means a request can sit for twenty
  minutes before the first tool call, and until now that looked
  exactly like a message nobody received.
- Best-effort, like every other status frame — a run is never worth
  losing over a badge.

## 0.11.0

- **A stop no longer swallows the queue.** The work queue lives in
  memory and the server broadcasts each `room.message` exactly once,
  so anything queued when systemd killed the cgroup was gone with no
  trace — the asker simply never heard back. Seen for real on
  2026-09-06: a restart armed at 19:16:37 while a request was still
  queued, and the request evaporated. SIGTERM/SIGINT now spool the
  in-flight task and everything behind it to
  `~/.cache/vroxy-dispatch/work-spool.json`, and the next process
  re-queues them before it even opens the socket, in the order they
  were asked.
- The IN-FLIGHT task is spooled first, deliberately: it was taken off
  the queue but never answered, so from the asker's side it is exactly
  as lost as the ones behind it, and it was asked first. Bounded to 50
  items and 30 minutes — replaying a half-hour-old question is worse
  than dropping it, since the answer arrives with no context.
- **A refused handshake is an ordinary event, not a crash.** Railway
  answers `502` for a few seconds mid-deploy;
  `websockets.InvalidStatusCode` isn't `ConnectionClosed`, so it fell
  through to the catch-all and was logged as "Unexpected error in
  cable loop" with a full traceback, then retried on a flat 5s instead
  of the backoff. Now caught as `websockets.WebSocketException`, which
  covers a rejected handshake as well as a dropped socket.
- **Room answers are threaded under the message that asked** — the
  final reply passed no `reply_to`, so answers floated free of their
  questions in a busy room. The timeout, crash and empty-response
  replies thread too.
- The test suite no longer writes into `log/dispatch.log`. It
  exercises the real reply and self-restart paths, so every run was
  filing fake "Room reply sent" lines into the operator's log and
  making the real history unreadable.

## 0.10.0

- **Dispatch restarts itself when a run edits its own checkout.**
  "@Dispatch ship a fix to yourself" left the process running the OLD
  code while the heartbeat reported the new version number. After every
  finished task it hashes the files systemd actually executes
  (`feedback_agent.py` + `bin/claude-chat`) against what it booted with
  — hashing bytes rather than reading `AGENT_VERSION` catches a fix
  shipped without a version bump, and an edit never committed.
- Four guards before it goes. It **waits for the work queue to drain**
  (that queue is in memory; restarting on top of it swallows the
  messages still on it), **byte-compiles what's on disk** (a restart
  into a SyntaxError is a crash loop — systemd hits the start limit and
  dispatch is off the air until a human notices; it says so in the room
  and stays on the old build instead), says one line that it's going so
  the gap doesn't read as having died, and writes a restart notice to
  `~/.cache/vroxy-dispatch/` because in-memory state can't survive the
  restart it describes.
- The restart is scheduled from OUTSIDE its own cgroup, with
  `systemd-run --on-active=5s --collect systemctl restart <unit>`.
  `systemctl restart` from inside would work, but systemd stops a unit
  by killing its whole cgroup — including any Claude still finishing.
  Falls back to exiting non-zero, and only when the unit's `Restart=`
  policy actually restarts on failure; under `Restart=no` that would
  take dispatch dark, so it reports the failure and keeps serving.
- The new process announces at boot, on `confirm_subscription` — the
  first moment the cable will accept a post: `✅ Back up on
  vroxy_dispatch 0.10.0 (abc1234) — was 0.9.0`, threaded under the
  message that asked. The notice is deleted on READ, before the post is
  attempted: every reconnect confirms the subscription again, and one
  that survived a read would be re-announced on each of them. Notices
  older than 15 minutes are dropped. A task with no room behind it
  restarts without announcing rather than guessing at one.
- 25 new tests, including that the queue-drain and compile guards
  actually bite.

## 0.9.0

- **Every run reports what it cost.** The CLI's `result` event carries
  `total_cost_usd`, `usage` and `num_turns`; all of it was being logged
  and thrown away. A new `room_run` action sends it to Rails at the end
  of a run (vroxy_web 2.78.0) along with the working log, which becomes
  a durable `DispatchRun` row — so spend can be shown per workspace and
  per person.
- The cost figure is reported, never derived. A real session on this box
  used 37.7M cache-read tokens against 19k output, and cache reads price
  at a fraction of fresh input, so multiplying tokens by a rate table
  would have been wrong by an order of magnitude.
- Sent AFTER the answer is posted, and best-effort: a failure to record
  a run must never look like a failure to reply. A timeout, a crash and
  an empty response each record too, marked as errors — a run that cost
  money and produced nothing is exactly the one worth seeing.

## 0.8.0

- **Reasoning reaches the room, not just tool calls.** `thinking`
  blocks were parsed and written to the log and nowhere else; they now
  go out as `room_progress` lines with `kind: thinking` alongside the
  tool lines. The final answer is deliberately not echoed into the
  trail — it arrives as the reply a second later, and saying it twice
  helps nobody.
- Rails keeps these for three days in Redis now (vroxy_web 2.76.0), so
  the cap went to 120 lines per run: a trail worth reopening tomorrow
  can afford to be longer than one that vanishes on refresh.

## 0.7.0

- **A room run reports what it's doing while it does it.** Each tool
  call sends a `room_progress` action (vroxy_web 2.75.0) carrying one
  glanceable line — the tool plus the argument that says what it
  touched, `Read(config/application.rb)`, never the whole input dict —
  tagged with the message that asked so the room UI can hang it under
  that message. Nothing is stored on either side.
- Capped at 60 lines per run and one line per tool call: the point is
  a glance, not a transcript. The send is best-effort and hops from
  the Claude thread to the event loop the same way the widget's
  progress chips already do — a progress line is never worth losing
  the answer over.

## 0.6.0

- **A screenshot posted in a room reaches the agent.** The envelope
  now carries each attachment's fetch URL (vroxy_web 2.72.0), so the
  files are downloaded before the prompt is built and listed in it by
  absolute path — Claude can only look at a picture that exists on
  disk. They land beside the session cache in
  `~/.cache/vroxy-attachments/<message>/`, never in the checkout,
  which would otherwise show up as untracked junk in the very diff
  the run is about to propose.
- Best-effort per file, and bounded: 5 files, 25 MB each, a 20-second
  fetch. A download that fails is logged and skipped — a screenshot
  we couldn't reach must not cost the room its answer. Filenames come
  from whoever uploaded them, so they're reduced to a safe basename
  before anything is written.

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
