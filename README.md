# vroxy

Two programs that talk to a [vroxy](https://vroxy.ai) workspace,
shipped together because they install together.

- **`vroxy`** — the CLI. Read and post to rooms, list workspaces,
  manage docs and tools, fire a dispatch from a terminal or a script.
  Pure stdlib, no dependencies.
- **`feedback_agent.py`** — the **dispatch agent**. A long-running
  ActionCable client that hands messages to a coding agent (Claude
  Code or Codex, headless), runs it in a throwaway git worktree, and
  streams the answer back into a vroxy room or support chat.

You want the CLI on your laptop. You want the dispatch agent on the
one machine that holds a checkout of the code it is allowed to change.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/VroxyAI/vroxy/main/install.sh | bash
```

That installs the CLI and then asks whether this machine should also
run a dispatch agent. Answer no and nothing privileged happens — the
CLI lands in `~/.local/bin` and that is the end of it.

Either half on its own:

```bash
./install.sh --cli        # just the CLI
./install.sh --dispatch   # just add a dispatch workspace
```

The CLI goes in via `pipx` when you have it, `pip install --user`
when you don't, and its own venv when the system python is externally
managed (PEP 668, which is most current distros). All three end with
`vroxy` on your PATH.

## The CLI

```bash
vroxy login                            # host + API token, stored 0600
vroxy workspaces                       # what you can reach
vroxy rooms <workspace>                # rooms in one
vroxy read <workspace> <room>          # recent messages
vroxy post <workspace> <room> "ship it"
vroxy dispatch <workspace>             # the workspace's dispatch agents
vroxy docs    <workspace> list|show|create|edit|publish|unpublish|delete
vroxy members <workspace> list|invite|role|remove|revoke
vroxy tools   <workspace> list|show|create|enable|disable|delete
```

Every command after `login` takes the workspace first — a hashid or
its slug.

`--json` goes before the subcommand and prints the raw payload, which
is the point of having a CLI at all — `vroxy --json rooms acme | jq`
beats a browser tab inside a script. Credentials live in
`~/.config/vroxy/credentials.json`; `VROXY_CONFIG_DIR` moves them.

## The dispatch agent

Everything below this line is the agent.

## Runtime shape

1. Connects `wss://vroxy.ai/cable?token=<VROXY_SERVICE_TOKEN>`.
2. Subscribes `{ channel: "AdminFeedbackChannel" }`. Rails resolves
   the token → `Tenant`-owned `ApiToken` → sets
   `current_tenant`, and the channel streams for exactly that
   tenant.
3. On `feedback.created` (fanned out by
   `POST /widget/feedback`) → builds a prompt → runs Claude **in a
   throwaway git worktree** → parses an optional fenced
   ` ```proposal ``` ` block → replies with the proposal and its
   diffstat. See [The proposal worktree](#the-proposal-worktree).
4. On `approve.requested` (operator clicked Apply) → writes the
   proposal files and either commits to the base branch or opens a
   PR, **as the server's ship policy directs**. See
   [Ship policy](#ship-policy).
5. On `room.message` → answers in a workspace **Room** as the
   dispatch bot user. See [Rooms](#rooms) below.
6. Heartbeat every 20 s; one in-flight worker at a time
   (Claude sessions aren't reentrant).

## The proposal worktree

The proposal phase runs Claude in a **throwaway git worktree of
HEAD**, not in your checkout. Before this, an investigation that
decided to edit a file did so in the real tree — so a card reading
"pending review" could be sitting on top of changes already written
to disk. Propose-then-approve is now literally true: nothing reaches
your working tree until you approve.

Where the worktree lives is load-bearing:

```
~/code/vroxy/                     ← CODE_ROOT
├── CLAUDE.md                     ← still loads (parent of the worktree)
├── vroxy_web/                    ← PROJECT
├── vroxy_dispatch/
└── .dispatch-vroxy_web-a1b2c3d4/ ← the worktree, a SIBLING
```

It is created beside the project inside `CODE_ROOT`, never in
`/tmp`, because that is what keeps a dispatch run's context identical
to a human's in this workspace: the parent `CODE_ROOT/CLAUDE.md`
still loads (Claude Code walks up from the working directory), and
sibling repos still resolve at `../vroxy_dispatch`. A `/tmp` worktree
silently loses both, and the model starts writing code that doesn't
match the workspace's rules.

Consequences worth knowing:

- **The run sees committed HEAD, not your uncommitted work.** That's
  the right default for a proposal, and it's why isolation is
  possible at all.
- **The diffstat is exact**, because the worktree started clean.
  That's what the ship policy sizes on.
- If Claude edits files but emits no fenced proposal, the worktree
  diff is turned into one rather than thrown away.
- Stale worktrees from a killed run are pruned on the next run.
- **Rooms are unaffected** — room mode edits your real checkout on
  purpose, because that's the point of asking it to change something.

## Code agents

Dispatch is one KIND of code agent, not the only one. A workspace
configures agents at `/w/:workspace/:slug/dispatch`:

- **`claude_code`** — local: a `vroxy_dispatch` process holding a
  checkout, reached over `AdminFeedbackChannel`. That's this program.
- **`copilot`** — remote: GitHub's agent API, which always ends in a
  pull request.

Every agent gets its own Room, so it's reachable from the web app,
the mobile app, or a widget chat that routed there. Each agent
answers to a `/slash-command` derived from its name (`Claude Code` →
`/claude-code`), which overrides a room's configured agent for one
message.

An agent has many **targets** — environments it works on, each with
its own base ref. For a local agent that's a checkout; for a remote
one it's a repository, and **one agent can cover several**. A room
picks which target it works on; with several and no pick, nothing is
guessed and the room says so.

## Ship policy

Applies to LOCAL agents (a remote one always opens a PR — that's all
Copilot produces).  The operator decides how an approved proposal
reaches the repo, per workspace and per target. Dispatch does not choose; it obeys the
decision that arrives in the `approve.requested` payload.

Configure at `/w/:workspace/:slug/settings` (workspace default) and
`/w/:workspace/:slug/dispatch` (per target). Targets appear on that
page automatically — dispatch reports its `PROJECT` on every
heartbeat, the server upserts a row, and the row is adopted by the
workspace's local agent when there is exactly one.

| Policy | What an approved proposal does |
| ------ | ------------------------------ |
| `always_ship` | commits to the base branch and pushes (auto-deploys) |
| `always_pr` | branches, pushes, opens a PR for you to merge |
| `auto` (default) | small → ship; large → PR |

`auto` routes to a **PR** when any of these is true:

- more files than `auto_pr_file_threshold` (default 3)
- more lines than `auto_pr_line_threshold` (default 80)
- any path matches `always_pr_paths` (default `db/migrate/*` — a
  schema change should never auto-land)
- the note explicitly asked for one ("PR", "branch", "for review")

Other settings: `base_ref`, `branch_prefix`, `pr_draft`, and
`auto_apply` — which, when on, ships a policy-sized-small change
with **no human clicking Apply**. Off by default. It never applies to
anything routed to a PR.

Every target field may be left blank to inherit the workspace's.
A target can also be disabled entirely, in which case dispatch
refuses to apply anything for it.

A PR comes back into the chat as a link card (`kind:
"pull_request"`), and the feedback stays `triaged` rather than
`resolved` — it isn't done until you merge.

## Rooms

Dispatch also sits in workspace chat rooms, so the team can just
talk to Claude Code where they already talk to each other. This is
conversation, not the feedback pipeline: no proposal blocks, no
Ship-it card, no tool chips in the room log.

**Turning it on** is per-room, by an operator, at
`/w/:workspace/:slug/rooms/:id/edit` → **Claude (dispatch)**:

| Mode                        | Dispatch answers                                     |
| --------------------------- | ---------------------------------------------------- |
| `off` (default)             | never                                                 |
| `mentioned`                 | only messages that `@Dispatch`                        |
| `all`                       | every human message in the room                       |

Rules that hold in both modes:

- **The bot's own messages never wake it** — that's the loop guard.
- **Webhook posts don't trigger a reply** unless they explicitly
  `@Dispatch`, so a build-notification room doesn't get answered
  once per deploy.
- Dispatch posts as the `dispatch@vroxy.ai` service user (`agent`
  flag, seated in the workspace at operator level and joined to the
  room). Replies go through `RoomMessageService`, so they fan out to
  the room cable, notifications, and outbound webhooks exactly like
  a human's message.
- **Each room gets its own Claude session**, separate from the
  feedback queue and from every other room.
- Long answers are split across several messages at the 4,000-char
  `RoomMessage` cap, on paragraph boundaries where possible.

**In-room commands:** `/reset` (aliases `/clear`, `/new`) drops that
room's Claude session and starts fresh. It answers immediately
without spending a Claude run.

### Asking the room a question

When Claude needs a decision it ends its reply with a fenced `ask`
block — JSON, same grammar as ` ```proposal ` on the feedback path:

````
Both work. Three files, no migration.

```ask
{"prompt": "Ship this to master or open a PR?",
 "mode": "one",
 "options": ["Ship to master", "Open a PR"]}
```
````

`mode` is `one` (buttons), `many` (checkboxes) or `text` (a free-text
box, `options` omitted). `options` take strings or
`{"label": …, "value": …}` pairs. The caps mirror `RoomAsk` on the
Rails side: 12 options, 120-char labels, 500-char prompt.

The fence never reaches the room — `parse_room_ask` strips it and
`ask_fallback` appends whatever the model's prose didn't already say,
as a numbered list, so the question is answerable by typing on
surfaces that draw no buttons (the widget, older mobile builds).

A block that isn't parseable, isn't a JSON object, is over 20 KB, has
no prompt, or offers no options in a non-`text` mode is **left in the
message as written**. Losing the reply to a bad fence would cost more
than an ugly one.

> **Not wired to buttons yet.** `AdminFeedbackChannel#room_ask` hangs
> the question on a message hashid, and nothing tells dispatch the
> hashid of the reply it just posted — `room_reply` returns nothing,
> there is no rooms endpoint on `/api/v1`, and `RoomChannel` refuses a
> connection with no `current_user`. Until `room_reply` echoes the
> posted hashid back, the prose fallback is what the room gets.

## Resetting a session

Dispatch keeps one Claude session per conversation so follow-ups
retain context. Two things go wrong with that, and each has a fix:

- **The session id outlived its transcript** (cleared history, a
  different host, a wiped `~/.claude`). `claude --resume` exits 1
  having produced nothing, which used to surface as *"Claude
  returned an empty response"* forever, because the dead id got
  written back after every failure. Dispatch now detects this,
  drops the id, and retries fresh automatically — no action needed.

- **The session is merely WRONG** — too long, wandered off, carrying
  stale assumptions. Nothing can detect that but you:

```bash
.venv/bin/python feedback_agent.py --reset            # feedback + every room, this PROJECT
.venv/bin/python feedback_agent.py --reset bazhjzyq   # one room, by hashid
.venv/bin/python feedback_agent.py --sessions         # list what's stored
```

`--reset` is safe to run while dispatch is live — the next message
just starts a new session. Session files live in
`~/.cache/claude-chat/`; `bin/claude-chat /clear` only clears the
non-streamed one, which is why it never fixed the streamed path.

## Installing a dispatch workspace

`./install.sh --dispatch` creates the venv, installs a systemd
TEMPLATE unit (`vroxy-dispatch@<workspace>.service`), asks for the
vroxy host and a workspace API token, **confirms the workspace by
name** before wiring anything to it, and writes
`/etc/vroxy-dispatch/<id>.env` at 0640.

```bash
./install.sh --dispatch   # add a workspace (interactive)
./install.sh --list       # what's installed, and whether it's up
./install.sh --update     # git pull + deps + restart every instance
./install.sh --remove ID  # stop, disable, forget one instance
```

**One process per workspace.** `AdminFeedbackChannel` streams for
exactly one tenant, so running dispatch for both vroxy and arubamu is
two units, not one process with two connections — which is why the
unit is a template and adding the second workspace costs one env file.

Each install generates a `VROXY_INSTALL_ID` once. The server keys the
agent row on it, so SEVERAL dispatches can also serve ONE workspace,
each with its own room and its own `@Name`. It is never regenerated:
rewriting it would orphan the agent row, its room, and its history.

The token needs `platform:dispatch` (or `full`) scope. `install.sh`
checks it against `GET /api/v1/whoami` and prints the workspace name
before asking you to confirm — installing an agent against the wrong
token is the kind of mistake you find a week later in someone else's
rooms.

### Manual install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x bin/claude-chat
```

Requires `claude` (Claude Code CLI) and `jq` on `PATH`.

## Provision the service token

Once on the Rails side (per-tenant):

```ruby
tenant = Tenant.find_by(public_key: "QY6F4nAPNBEpwBlfsMAL6tP2")
token  = tenant.api_tokens.new(name: "vroxy_dispatch",
                                scopes: ["platform:dispatch"])
token.generate!
token.save!
puts token.raw_token  # ← ONLY chance to see it; store in env immediately
```

The `raw_token` is 48 chars of `SecureRandom.alphanumeric`. Copy it
into the dispatch host's environment as `VROXY_SERVICE_TOKEN`.
Rotate by `token.revoke!` and creating a new one.

## Run (foreground)

```bash
VROXY_SERVICE_TOKEN=<token>  .venv/bin/python feedback_agent.py
```

`Subscribed to AdminFeedbackChannel` in the log means dispatch is
ready. `reject_subscription` means the token is wrong scope or
attached to a User instead of a Tenant.

### Environment

| Var                     | Default                             |
| ----------------------- | ----------------------------------- |
| `VROXY_CABLE_URL`     | `wss://vroxy.ai/cable`            |
| `VROXY_SERVICE_TOKEN` | *(required)*                        |
| `CODE_ROOT`             | parent of this checkout             |
| `PROJECT`               | `vroxy_web`                       |
| `DISPATCH_ENGINE`       | `claude` (or `codex`, `cursor`)     |
| `DISPATCH_TELEMETRY`    | `1` (set `0` to stop reporting the box) |
| `CLAUDE_CHAT_BIN`       | `./bin/claude-chat`                 |
| `CODEX_BIN`             | `codex` on PATH                     |
| `CLAUDE_STREAM`         | `1` (set `0` to skip streamed path) |
| `LOG_LEVEL`             | `INFO`                              |
| `LOG_FILE`              | `./log/dispatch.log` (`""` disables)|
| `LOG_MAX_BYTES`         | `10485760` (10 MB before rotating)  |
| `LOG_BACKUP_COUNT`      | `5`                                 |
| `VROXY_DISPATCH_UNIT`   | `vroxy-dispatch-feedback-agent.service` |
| `VROXY_DISPATCH_RESTART_DELAY` | `5` (seconds before a self-restart fires) |
| `VROXY_DISPATCH_STATE_DIR` | `~/.cache/vroxy-dispatch`        |

## Which CLI does the work

`DISPATCH_ENGINE` picks the engine for the whole process: `claude`
(the default), `codex` for OpenAI's Codex CLI, or `cursor` for
`cursor-agent`. One instance runs one engine — to have several,
install a second systemd instance with its own `DISPATCH_ENGINE`, the
same way a second project gets its own. An unrecognised value raises
at the first run rather than falling back, because a typo that
silently ran the other model is worse than a loud failure.

`gemini`, `copilot_cli`, `opencode` and `amp` also have runners, but
only `claude`, `codex`, `cursor` and `copilot_cli` have ever executed
here — the rest are written from their `--help` surfaces.

The engine is reported in the heartbeat as `meta.engine`, and the
server registers this install as a `claude_code`, `codex` or `cursor`
DispatchAgent accordingly — which is what makes it @-mentionable
under its own name. Flipping `DISPATCH_ENGINE` on an existing install
MOVES that agent rather than creating a second one, so the room and
its history follow.

Every local agent in a workspace subscribes to the same tenant
channel, so a box running both engines sees each room message twice.
The broadcast names the agent it was routed to and each instance
ignores the other's work; without that they would both answer.

Both engines produce the SAME event vocabulary — `tool_use`,
`text_delta`, `thinking`, `result`, `stalled` — so the progress
trail, the room reply, the proposal flow and the stall kill are
shared and know nothing about which CLI is behind them. Adding a
third means one runner and one branch in `run_agent_streamed`.

What differs, deliberately:

- **Session resume.** Claude stores a session id and resumes with
  `--resume`; Codex stores a `thread_id` and resumes with
  `codex exec resume <id>`. Both keep it in the same `SID_DIR`, and
  Codex's is prefixed `codex_` so switching engines can't hand a
  thread id to the wrong CLI.
- **The answer.** Claude's `result` event carries the final text.
  Codex has no such event: the LAST `agent_message` is the answer and
  the ones before it are narration the trail has already shown, so
  joining them all would repeat the commentary inside the reply.
- **Cost.** Claude reports `total_cost_usd` per run. Codex bills
  against the signed-in plan and reports no price, so `cost_usd` is
  null rather than a number we invented. Token usage is reported by
  both.
- **Sandboxing.** Codex runs with
  `--dangerously-bypass-approvals-and-sandbox`, the counterpart of
  claude's `--dangerously-skip-permissions`. The disposable worktree
  is the trust boundary in both cases, and Codex cannot nest its own
  sandbox inside the one this box already runs under — without the
  bypass every shell call returns "Operation not permitted" and the
  model reports failure instead of working.

## Harness inventory

Every heartbeat carries `meta.harnesses` — which coding CLIs this box
has, with versions:

```json
[{"id": "claude", "label": "Claude Code", "version": "2.1.233 (Claude Code)",
  "engine": "claude", "active": true},
 {"id": "codex", "label": "OpenAI Codex", "version": "codex-cli 0.153.4",
  "engine": "codex", "active": false}]
```

`engine` is the `DISPATCH_ENGINE` value that drives it, absent for one
we can only report on — knowing Gemini is installed is useful before
we can run it. `active` marks the one this instance is running. The
server stores the list on the agent row and shows it at
`/admin/mgmt/dispatch_agents/:id`.

### Turning it off

It is on by default, because a fleet view of what is installed where
is the point. But it is somebody's machine, so:

```bash
DISPATCH_TELEMETRY=0
```

stops the probe entirely — no subprocesses are spawned — and the
heartbeat carries `meta.telemetry: false` instead of an inventory.

That flag is an explicit opt-out, NOT a missing key, and the
difference is load-bearing. A heartbeat that simply says nothing
about harnesses is read as "this build predates the feature" and the
server keeps the last inventory it had. `telemetry: false` makes the
server **delete** what it already stored. Disabling telemetry has to
mean the data stops existing, not that it stops being refreshed.

Nothing else in the heartbeat is affected: status, project, engine,
version and install id still go up, because those are what make the
agent reachable and routable rather than observations about the host.

Probed at startup and refreshed every 15 minutes, in a thread, so
installing a CLI does not need a restart and a slow `--version` cannot
stall the socket. Add one to `KNOWN_HARNESSES`; `<ID>_BIN` overrides
PATH the same way `CLAUDE_BIN` and `CODEX_BIN` do.

## Watching what it's doing

Every run logs to the terminal **and** to a rotating file, so a
dispatch started in a shell stays readable after that shell is gone:

```bash
tail -f log/dispatch.log            # follow live
grep -E "Handling|tool_use|Reply|Room reply" log/dispatch.log   # just the beats
```

The log records each turn end to end: the triggering message, the
prompt's working directory and session file, every `tool_use` and its
result preview, the text Claude produced, token usage, and the reply
that went back. `LOG_LEVEL=DEBUG` adds the ignored cable frames.

`log/` is gitignored. Under systemd the units also append to
`/var/log/vroxy-dispatch/`, which is now redundant but harmless.

## The 90-second ceiling

Nothing in this process waits on a response for longer than
`STALL_SECONDS` (90, override with `VROXY_STALL_SECONDS`). Someone is
watching a typing indicator on the other end, so a wait they can't see
the end of is the worst failure mode available.

- **The streamed run is watched for silence.** A daemon thread does the
  blocking `proc.stdout` read; the run loop waits on a queue it can
  time out. Before this there was no timeout at all — a wedged claude
  (a hung tool call, a dead network read) held dispatch open forever
  and the asker never heard back.
- **Silence is reported, not swallowed.** Each elapsed window emits a
  `stalled` event that the progress trail renders into the room
  ("still working — nothing back for 90s").
- **Wedged gets killed.** After `STALL_WINDOWS_BEFORE_KILL` (4,
  `VROXY_STALL_WINDOWS`) consecutive silent windows the run is wedged
  rather than slow: the process group is killed (SIGTERM → SIGKILL) and
  any partial output comes back with a note saying it was cut short.
  The subprocess runs in its own session so that kill can't reach
  dispatch itself.
- **The blind-wait paths get a flat cap.** The non-streamed fallback
  and the git helper have no stream to watch, so they share
  `SUBPROCESS_HARD_CAP_SECONDS` (`STALL_SECONDS ×
  STALL_WINDOWS_BEFORE_KILL`). `StallCeilingTest` fails if any timeout
  literal in the module climbs back over it.
- **The room prompt teaches Claude the same rule**: every Bash call
  gets `timeout: 90000` or less, run only the tests that cover the
  change (never a bare `bin/system-test` or `bash ./test.sh`), and
  background-and-poll anything genuinely long instead of raising a
  timeout.

If you hit the ceiling, the fix is a different approach — not a bigger
number.

## Run against local dev

`ctovibe_web` runs under `docker compose`, and its compose file maps
the web container to **host port 3002** (`docker-compose.yml`,
`"3002:3000"`) so it can coexist with the sibling vroxy stack. So the
local cable URL is `ws://localhost:3002/cable` — note plain `ws://`,
not `wss://`. That's load-bearing: `main()` derives the WS `Origin`
header from the cable URL's scheme, and Rails' development
`allowed_request_origins` only matches `http://localhost:<port>`. A
`wss://` URL against local dev synthesizes `https://…` and the
handshake comes back 404.

Bring the app up first (`docker compose up` in `ctovibe_web`), then:

```bash
cd ~/code/ctovibe/ctovibe_dispatch
VROXY_CABLE_URL=ws://localhost:3002/cable \
VROXY_SERVICE_TOKEN=4VK25VuTFzU830vhHG8AybL1v7Yfmjdgt5n5FcPMUInZxWkD \
PYTHONUNBUFFERED=1 \
python3 feedback_agent.py
```

`CODE_ROOT` / `PROJECT` need no override — they already default to
the sibling `ctovibe_web` checkout. `python3` works without the venv
if your system Python already satisfies `requirements.txt`
(`websockets>=12,<14`); use `.venv/bin/python` otherwise.

### The local dispatch token

`db/seeds/vroxy_tenant.rb` seeds the `vroxy` tenant (slug renamed from ctovibe by migration)
(`public_key: w8eYmQ8uPppj2BgEBywUSaoz`) with a `seed dashboard
token`, but that one is scoped `tenant:read tenant:write` —
`AdminFeedbackChannel#subscribed` rejects it. Dispatch needs `full`
or `platform:dispatch`, so mint a second token:

```bash
docker compose exec web bin/rails runner '
t = Tenant.find_by!(slug: "vroxy")
t.api_tokens.where(name: "vroxy_dispatch dev").where(revoked_at: nil).find_each(&:revoke!)
puts ApiToken.generate!(owner: t, name: "vroxy_dispatch dev",
                        scopes: ["platform:dispatch"]).raw_token'
```

The raw token is `SecureRandom.alphanumeric` — the value baked into
the command above is this machine's, and a `db:reset` (or a fresh
checkout) invalidates it. Re-run the minting command and paste the
new value in. Revoke when you're done:

```bash
docker compose exec web bin/rails runner \
  'ApiToken.find_by(name: "vroxy_dispatch dev")&.revoke!'
```

The seeded tenant's `origin_allowlist` is empty, so
`Connection#origin_ok?` short-circuits true and `http://localhost:3002`
passes. If you populate the allowlist for widget testing, add that
origin or dispatch starts getting rejected at connect.

### Local dev doesn't sandbox the ship path

Pointing `VROXY_CABLE_URL` at localhost only redirects the cable.
The PROPOSAL phase is now safe everywhere — it runs in a throwaway
worktree — but an APPROVED proposal still runs a real `git commit` +
`git push` in `CODE_ROOT/PROJECT`, and a PR-routed one still shells
out to `gh pr create` against the real remote. Local dev is a safe
place to exercise `feedback.created` → `reply`; it is not a safe
place to click Apply unless you mean it.

## Run (systemd, recommended)

The unit installed on the dispatch host is
`vroxy-dispatch-feedback-agent.service`, reading
`/etc/default/vroxy-dispatch`.

> **If it hangs at "Connecting" and never says "Subscribed", the
> token is wrong.** A rejected connect closes the socket without a
> frame, which looks exactly like a stall from the client side. Check
> the server: `[vroxy.cable] connect REJECT reason=unauthenticated`
> means the token isn't a live `Tenant`-owned `ApiToken` with
> `platform:dispatch` or `full`. Mint a new one (below) rather than
> guessing — a token carried over from before the rename authenticates
> against nothing.

`/etc/default/vroxy-dispatch`:

```ini
VROXY_CABLE_URL=wss://vroxy.ai/cable
VROXY_SERVICE_TOKEN=REPLACE_ME

CODE_ROOT=/home/ubuntu/code/vroxy
PROJECT=vroxy_web

LOG_LEVEL=INFO
PYTHONUNBUFFERED=1
```

`/etc/systemd/system/vroxy-dispatch-feedback-agent.service`:

```ini
[Unit]
Description=vroxy dispatch — AdminFeedbackChannel cable subscriber (feedback_agent.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/code/vroxy/vroxy_dispatch
EnvironmentFile=/etc/default/vroxy-dispatch
ExecStart=/home/ubuntu/code/vroxy/vroxy_dispatch/.venv/bin/python /home/ubuntu/code/vroxy/vroxy_dispatch/feedback_agent.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vroxy-dispatch-feedback-agent
sudo journalctl -u vroxy-dispatch-feedback-agent -f
# or the agent's own rotating file:
tail -f /home/ubuntu/code/vroxy/vroxy_dispatch/log/dispatch.log
```

There is no `server.py` in this repo — if you find a
`vroxy-dispatch-server.service` on a host, it is a leftover pointing
at code that no longer exists and should be removed.

## Self-update

Dispatch edits its own checkout often — "@Dispatch ship a fix to
yourself" is a normal Tuesday. The process that runs the fix is
still running the OLD code, and keeps answering from it while the
heartbeat reports the new version number. So after every finished
task it hashes the files systemd actually executes
(`feedback_agent.py` + `bin/claude-chat`) and compares them to what
it booted with. Hashing bytes rather than reading `AGENT_VERSION`
catches a fix that shipped without a version bump, and an edit that
was never committed.

When they differ:

1. **Wait for the work queue to drain.** The queue is in memory;
   restarting on top of it swallows the messages still on it.
2. **Byte-compile what's on disk.** A restart into a `SyntaxError`
   is a crash loop — systemd restarts on failure, hits the start
   limit, and dispatch is off the air until a human notices. If it
   won't compile, dispatch says so in the room and stays on the old
   build.
3. **Say it's going.** One line in the room that asked, so the gap
   doesn't read as dispatch having died.
4. **Write a restart notice** to `$VROXY_DISPATCH_STATE_DIR` — the
   room, the message to thread under, and the version it's leaving.
   In-memory state does not survive the restart it describes.
5. **Schedule the restart from outside its own cgroup**, with
   `systemd-run --on-active=5s --collect systemctl restart <unit>`.
   `systemctl restart` from inside would work, but systemd stops a
   unit by killing its whole cgroup — including any Claude process
   still finishing and the shell that issued the command. If
   `systemd-run` isn't available, it falls back to exiting non-zero,
   and only when the unit's `Restart=` policy actually restarts on
   failure. Neither working means it reports that in the room and
   keeps serving stale rather than going dark.

The next process reads the notice on `confirm_subscription`, posts
"✅ Back up on X — was Y", and deletes it. The notice is deleted on
READ, before the post is attempted: every reconnect confirms the
subscription again, and a notice that survived one read would be
re-announced on each of them.

A task with no room behind it (an approved proposal, say) restarts
without announcing — there is no room to speak in, and picking one
would be a guess.

Requires passwordless `sudo systemd-run` for the service user, or
`Restart=on-failure` on the unit (the unit above has it).

## One tenant per process

Vroxy's `AdminFeedbackChannel` streams for exactly one tenant
(the one the auth token belongs to). Multi-tenant hosting means
running one systemd unit per tenant (e.g.
`vroxy-dispatch-<tenant_slug>.service`) with a per-tenant
`VROXY_SERVICE_TOKEN` in the `EnvironmentFile`. See the vroxy
version if you need a global-scope alternative.

## Wire contract

### Server → dispatch

`AdminFeedbackChannel` broadcasts these `message.type` values:

- **`feedback.created`** — envelope: `{ feedback: {...}, chat:
  {hashid, title}, message: {hashid, body} }`. Dispatch replies
  via `reply`.
- **`feedback.followup`** — visitor posted a follow-up on an
  existing feedback chat. Dispatch re-enters `handle_feedback`
  with the fresh state.
- **`approve.requested`** — operator clicked Apply on a proposal
  card (or `auto_apply` fired). Carries `policy: { mode: "ship" |
  "pr" | "none", base_ref, branch_prefix, pr_draft, reason }` — the
  server's resolved decision, which dispatch obeys. The proposal's
  own `mode` is only a fallback for a server that predates this.
- **`room.message`** — a message in a dispatch-enabled Room that
  passed `Room#dispatch_should_answer?`. Envelope: `{ room:
  {hashid, name, topic, dispatch_mode}, message: {hashid, body,
  created_at}, sender: {hashid, name, source}, history: [...] }`.
  `history` is up to 30 prior turns, oldest first, excluding the
  triggering message.

### Dispatch → server

Dispatch calls these `action`s on the channel:

| Action    | Payload                                     | Server does                                                                          |
| --------- | ------------------------------------------- | ------------------------------------------------------------------------------------ |
| heartbeat   | `version`, `meta`                         | `Rails.cache.write("vroxy_dispatch:heartbeat:<tenant.id>", {...}, expires_in: 60)` |
| progress    | `chat_id`, `name`, `input`                | Persists a `tool_call` `SupportChatMessage` + broadcasts a `tool` chip on `SupportChatChannel` |
| reply       | `chat_id`, `body`, `kind` (opt), `proposal` (opt), `pull_request` (opt) | Persists an assistant `SupportChatMessage` + broadcasts `done`; stamps the feedback id and the resolved policy onto the message; bumps linked feedback `open → triaged`; fires auto-apply when the policy allows |
| room_reply  | `room_id`, `body`, `reply_to` (opt)       | Posts a `RoomMessage` as the dispatch bot via `RoomMessageService` (refused unless the room has dispatch on) |
| room_typing | `room_id`                                 | Ephemeral `typing` frame on the room's `RoomChannel`; nothing persists |

## Feedback source

Feedback lands via `POST /widget/feedback`
([`app/controllers/widget/feedbacks_controller.rb`](../vroxy_web/app/controllers/widget/feedbacks_controller.rb))
which creates the `AdminUiFeedback` + a linked `SupportChat` and
calls `AdminFeedbackChannel.broadcast_feedback_created`.

The client-side picker that populates the payload lives in
`vroxy_web/app/javascript/admin_ui_inspector.js`.

## Tests

```bash
python3 -m unittest test_feedback_agent      # the dispatch agent
python3 -m unittest discover -s tests        # the CLI
```

Covers `build_prompt` (full / page-level / sparse / duplicate-
partial cases), `parse_proposal` (plain / inline_ship /
pull_request / malformed JSON) and `parse_room_ask` +
`ask_fallback` (valid / malformed / absent / 30 options / caps /
prose that already restates the question).

## See also

- [CHANGELOG.md](./CHANGELOG.md)
- Reference implementation:
  `../vroxy_cuh/vroxy_dispatch/feedback_agent.py`
