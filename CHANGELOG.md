# Changelog

## 0.33.0

### Changed

- **`vroxy_cli` merged in — one repo, one installer, two programs.**
  `install.sh` now installs the `vroxy` CLI and then asks whether this
  machine should also run a dispatch agent, so a laptop gets the CLI
  without anything privileged happening. `--cli` and `--dispatch`
  install one half. The CLI stays dependency-free (websockets sits
  behind a `dispatch` extra) and falls back to its own venv when the
  system python is externally managed, which PEP 668 makes the norm on
  current distros. `feedback_agent.py`, `requirements.txt` and
  `install.sh` keep their root paths, so a `git pull` on an existing
  systemd install is uneventful.
- The repo is now `VroxyAI/vroxy`. GitHub redirects the old name.
- One version number across the CLI, the agent, and the package.

### Fixed

- **`curl … | bash` actually works.** Piped, there is no script on disk,
  so `BASH_SOURCE` resolved to the caller's current directory and every
  `$HERE/...` path pointed at whatever folder they were standing in —
  `pip install "$HERE"` would have tried to install that. The installer
  now detects it has no checkout, clones one into `~/.local/share/vroxy`
  (`VROXY_HOME`), and re-execs inside it. `--help` reads the same path
  rather than a `BASH_SOURCE` that does not exist there.
- A virtualenv that fails to build is removed rather than left half-made
  for the next run to trip over, and says `apt install python3-venv`,
  which is the actual cause on Debian.

## 0.32.0

### Added

- The heartbeat reports `meta.model` — the model the CLI actually used
  on the last run, captured from the stream's `assistant` events
  rather than read from config. Config says what was asked for; the
  stream says what answered, and an alias like `opus[1m]` only
  resolves at run time. Rides under `DISPATCH_TELEMETRY` with the
  harness inventory and hostname, so opting out erases it server-side.
- It follows that the value is absent until the first run of a
  process. That is deliberate: reporting a configured name that never
  ran would be a guess dressed as a fact.

## 0.31.0

### Added

- The heartbeat now reports `meta.hostname` (`socket.gethostname()`),
  so the room can say which box a run is on next to which harness is
  driving it. It sits inside the `DISPATCH_TELEMETRY` branch with the
  harness inventory — it names somebody's machine, so the opt-out has
  to take it too, and on the server an opt-out erases the stored value
  rather than freezing it.

### Fixed

- `AGENT_VERSION` was left at `0.29.1` when 0.30.0 shipped, so every
  heartbeat under-reported its own version and the admin showed a
  build that wasn't running. Now `0.31.0`.

## 0.30.0

### Added

- **`./install.sh --unattended`.** The interactive path is six `read
  -rp` prompts deep, so nothing could install dispatch without a
  terminal — which made a cloud image impossible. The new flag takes
  `VROXY_TOKEN` (required), `VROXY_HOST`, `CODE_ROOT`, `PROJECT`,
  `VROXY_AGENT_NAME`, `VROXY_INSTANCE_ID` and `DISPATCH_ENGINE` from
  the environment and never from argv, which is world-readable. It
  still verifies the token against `/api/v1/whoami` before writing
  anything. Used by `vroxy_infra`'s cloud-init.

### Fixed

- **Reinstalling a workspace minted a new `VROXY_INSTALL_ID`.** The
  comment above it said "generated once and never regenerated", but
  overwriting an existing install wrote a fresh one, orphaning the
  agent row, its room and its history on the server. The env file is
  now read back for an existing id first. This mattered little for a
  hand-run installer and matters a lot for cloud-init, which re-runs
  on every boot.

### Changed

- Env-file writing moved into `write_env_file`, shared by the
  interactive and unattended paths so the two cannot drift.

## 0.29.1

### Fixed

- **`install.sh` couldn't install.** `ensure_venv` ran the venv's
  `pip` script, whose shebang hardcodes the absolute path of the venv
  that CREATED it. A checkout whose `.venv` was copied from elsewhere
  (this box's came from `ctovibe_dispatch`) has a `pip` that cannot
  execute at all — `required file not found`, naming a directory that
  no longer exists. Now `python -m pip`, which has no shebang to
  break.
- **`--list` reported "Nothing installed yet" over a running
  instance.** `ENV_DIR` is `0750 root:root` because the env files
  hold workspace tokens, so an unprivileged `find` read nothing and
  the error was swallowed by `2>/dev/null`. It reads the directory
  under `sudo -n` now — listing instance names isn't privileged,
  reading the files is.

## 0.29.0

### Added

- **An operator can hold queued work, and stop a run that has already
  started.** The server sends `room.pause` / `room.resume` /
  `room.kill`; this process acts on them, because the work queue lives
  in ITS memory and the server cannot reach into a queue it does not
  own.
- **A held item is re-queued at the BACK, never dropped.** Pausing is
  a request to wait, not to discard, so the answer still arrives when
  it is released. Re-checked every `PAUSED_REQUEUE_DELAY_SECONDS` (5),
  well under `STALL_SECONDS` so a queue of held work never reads as a
  wedged process.
- **A stop keeps the Claude session id, which is what makes it a pause
  rather than a discard.** A killed run exits non-zero, and the normal
  path only persists the session id when the run did NOT fail — so
  without this, every "resume" would have silently started a fresh
  session with no memory of the work. `run_claude_streamed` now writes
  the id on a deliberate cancel too.
- A stopped run posts nothing. It is held and re-queued instead, so
  resuming answers the question rather than apologising for a crash
  that the operator asked for.

### Changed

- `cancel_key` is the LAST parameter on `run_claude_streamed`,
  `run_codex_streamed` and `run_agent_streamed`. `run_agent_streamed`
  forwards positionally, so a parameter inserted anywhere else shifts
  `allow_resume` into it — there is a test asserting the position.

## 0.28.0

### Fixed

- **The working log in the web showed the same line over and over.**
  `_progress_line` took `value.splitlines()[0]` — the first line of a
  tool's command — and nearly every command opens by `cd`-ing to the
  checkout, so a whole session rendered as
  `Bash(cd /home/ubuntu/code/vroxy/vroxy_web)` repeated, with the
  actual work discarded. The terminal log was right the whole time
  because it already went through `_one_line`; only the room trail
  was lying.
- It now squashes the WHOLE value onto one line through that same
  helper, so the two surfaces can't disagree about what ran. The
  existing test asserted the broken behaviour — it was green for the
  wrong reason — and has been rewritten alongside a regression case
  built from two real `cd`-prefixed commands that used to render
  identically.

## 0.27.0

### Added

- **`DISPATCH_TELEMETRY=0` turns the harness inventory off.** On by
  default. When off the probe never runs — no subprocesses at all —
  and the heartbeat sends `meta.telemetry: false`, which makes the
  server DELETE the inventory it already holds rather than freeze it.
  A heartbeat that just says nothing is still read as an older build
  and keeps its last inventory; the explicit false is what
  distinguishes "I opted out" from "I can't tell you". Status,
  project, engine, version and install id are unaffected — those are
  what make the agent reachable, not observations about the host.

## 0.26.0

### Added

- **Every `room_run` names the engine that produced it.** The server
  stamps it on the `DispatchRun` row rather than looking it up
  through the agent, so per-harness cost and token numbers stay true
  after someone flips `DISPATCH_ENGINE` — that flip moves the agent
  row, and without this every past run would re-attribute itself to
  the new engine.

## 0.25.0

### Added

- **Pi joins the detected harnesses.** OpenCode was already in the
  probe from 0.24.0, versions included; `pi` was not. Both are
  detected and version-reported, neither is drivable yet — writing a
  runner means learning a CLI's headless event stream from the real
  binary, and neither is installed on this box.
- A test asserts no two known harnesses share a binary name, which
  would report one install twice.

## 0.24.0

### Added

- **Reports which coding CLIs are installed on this box.** The
  heartbeat carries `meta.harnesses`: Claude Code, Codex, Gemini,
  GitHub Copilot CLI, Aider, OpenCode, Cursor Agent, Amp and Goose,
  each with its `--version` and a flag for the one this instance
  actually runs. The server stores it on the agent row, so
  "what could this checkout be driven with?" is answerable from
  `/admin/mgmt` without shelling in.
- A CLI that resolves but will not report a version is still listed
  without one — installed-but-broken is a different problem from not
  installed, and flattening the two into "absent" hides the one worth
  fixing.
- The probe is cached for 15 minutes rather than run on every 20s
  heartbeat, and refreshes on that timer so a CLI installed while
  dispatch is running turns up without a restart. It runs in a thread:
  a slow `--version` must never stall the socket, and a full sweep of
  every known harness stays well inside `STALL_SECONDS` — asserted by
  a test rather than assumed.
- `<ID>_BIN` overrides are honoured, so a CLI installed off PATH is
  found the same way `CLAUDE_BIN` and `CODEX_BIN` already work.

## 0.23.0

### Added

- **The heartbeat reports `meta.engine`.** The server keys the agent
  row on it, so a `DISPATCH_ENGINE=codex` instance registers as a
  `codex` DispatchAgent rather than as Claude Code — that is what
  makes it @-mentionable under its own name in a room.
- **Work addressed to another engine is left alone.** Every local
  agent in a workspace subscribes to the same tenant channel, so a
  box running one Claude instance and one Codex instance sees each
  room message twice and, before this, both would have answered. The
  server already named the agent it routed to; `_is_ours` reads that
  kind and returns early when it is not this engine's.
- A frame carrying no agent block is still answered, so an older
  server does not go silent against a newer dispatch.

## 0.22.0

### Added

- **OpenAI's Codex CLI as a second engine.** `DISPATCH_ENGINE=codex`
  runs `codex exec --json` instead of `claude -p`; `claude` stays the
  default and nothing changes for an existing install. An
  unrecognised value raises rather than falling back, so a typo can't
  quietly run the other model.
- `run_codex_streamed` mirrors `run_claude_streamed` exactly — same
  signature, same normalised events (`tool_use` / `text_delta` /
  `thinking` / `result` / `stalled`), same `STALL_SECONDS` kill, same
  stale-session retry, same return of the final answer. The progress
  trail, room reply and proposal flow needed no branch; the only
  place that knows there are two engines is `run_agent_streamed`.
- Codex's stream is mapped from what it actually emits, verified
  against real runs: `thread.started` carries the resumable
  `thread_id`, `item.started`/`item.completed` carry an `item.type`
  (`command_execution` announced on START so the room sees a command
  as it runs, `agent_message`, `reasoning`), and `turn.completed`
  carries usage. An item kind this build predates is reported under
  its own name rather than dropped.
- The LAST `agent_message` is the answer — Codex has no `result`
  event, and joining every message would repeat the narration the
  trail already showed.
- `cost_usd` is null for Codex: it bills the signed-in plan and
  reports no per-run price, so there is nothing honest to put there.
- stdin is `DEVNULL` for Codex runs — it reads stdin when it is
  attached, which under a service manager is a way to hang.

## 0.21.0

### Removed

- **Dispatch no longer claims to be typing.** `room_typing_forever`
  kept a human "someone is composing a reply" indicator alive for the
  length of every run. A bot has the queued/working badge, the live
  progress trail, and an elapsed clock to say it is busy; borrowing
  the human signal claimed a presence it doesn't have.
- The server drops the frame too, so an older dispatch build stops
  showing it the moment vroxy_web 2.95.0 is deployed rather than
  waiting for the agent to update.

## 0.20.0

### Added

- **Editing a message that is still queued updates the queued task.**
  Press up, fix the typo, and the agent reads what you meant rather
  than what you first sent. The server sends `room.message.edited`
  only while the run is still queued, and `apply_queued_edit` rewrites
  the matching item, rebuilding the queue in order so nobody's
  conversation gets reordered.
- Once a run is in flight the edit is ignored on purpose: Claude is
  already reading the old words, and swapping them would be a lie
  about what it ran.

## 0.19.0

### Fixed

- **A busy agent never updated itself.** `restart_if_self_updated`
  waited for the work queue to drain before restarting, and on an
  agent that keeps being asked things the queue never drains — every
  check logged "holding the restart" while more work arrived. 0.17.0
  and 0.18.0 were both committed, pushed, and still not running hours
  later for exactly this reason.
- It now restarts between tasks regardless of what is queued.
  `spool_pending_work` already writes the in-flight task and the whole
  queue to disk on shutdown, and the next process replays them, so a
  mid-queue restart costs a short delay rather than a lost message —
  which is what the drain guard was written to prevent before the
  spool existed.

## 0.18.0

### Added

- **Every log line now says which request it belongs to.** Tailing
  `log/dispatch.log` showed a wall of `→ tool_use` lines with no way to
  tell what was being worked on — the "Handling room message" header
  scrolls away in seconds and never comes back. Lines now carry a task
  tag: `[#Claude is there a limit on the web working log] → tool_use
  Bash(...)`, and `[idle]` when nothing is in flight.
- The tag is a `contextvars` value set when a task starts and cleared in
  the worker's `finally`, so it survives `asyncio.to_thread` — which is
  where the tool_use lines are actually logged from. A tag that did not
  propagate across that boundary would leave exactly the lines you want
  labelled as the only unlabelled ones, so there is a test for it.

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
