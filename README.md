# vroxy_dispatch

An ActionCable client that lets an admin (or a widget visitor's
`/note` slash command) pass messages to **Claude Code (headless)**
and stream the response back into vroxy's support chat.

Ported from the walkie-talkie app's dispatch agent (that product has
since been renamed cuh; its web repo is `wartron/cuh_web`) with
adjustments for vroxy's tenant-scoped channel + hashid message
identifiers. Deployment note: this same host previously ran the
walkie-talkie's dispatch under the `vroxy-dispatch-*` unit names —
those stale units must be stopped/disabled before installing the
units below (they run deleted code and hold the old product's
service token).

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

## Ship policy

The operator decides how an approved proposal reaches the repo, per
workspace and per project. Dispatch does not choose; it obeys the
decision that arrives in the `approve.requested` payload.

Configure at `/w/:workspace/:slug/settings` (workspace default) and
`/w/:workspace/:slug/dispatch` (per project). Projects appear on that
page automatically — dispatch reports its `PROJECT` on every
heartbeat and the server upserts a row.

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

Every project field may be left blank to inherit the workspace's.
A project can also be disabled entirely, in which case dispatch
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

## Install

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
| `CLAUDE_CHAT_BIN`       | `./bin/claude-chat`                 |
| `CLAUDE_STREAM`         | `1` (set `0` to skip streamed path) |
| `LOG_LEVEL`             | `INFO`                              |
| `LOG_FILE`              | `./log/dispatch.log` (`""` disables)|
| `LOG_MAX_BYTES`         | `10485760` (10 MB before rotating)  |
| `LOG_BACKUP_COUNT`      | `5`                                 |

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

> **Rename migration note:** the dispatch host currently runs the
> pre-rename units (`ctovibe-dispatch*.service` reading
> `/etc/default/ctovibe-dispatch`, checkout under
> `/home/ubuntu/code/ctovibe`). The live units get migrated to the
> `vroxy-dispatch*` names below at deploy time (rename plan B6.3):
> install the new unit + env file with values copied over, disable
> the `ctovibe-dispatch*` units, verify heartbeats.

`/etc/default/vroxy-dispatch`:

```ini
VROXY_CABLE_URL=wss://vroxy.ai/cable
VROXY_SERVICE_TOKEN=REPLACE_ME

CODE_ROOT=/home/ubuntu/code/vroxy
PROJECT=vroxy_web

LOG_LEVEL=INFO
PYTHONUNBUFFERED=1
```

`/etc/systemd/system/vroxy-dispatch.service`:

```ini
[Unit]
Description=vroxy dispatch — AdminFeedbackChannel cable subscriber
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
sudo systemctl enable --now vroxy-dispatch
sudo journalctl -u vroxy-dispatch -f
```

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
python3 -m unittest test_feedback_agent
```

Covers `build_prompt` (full / page-level / sparse / duplicate-
partial cases) and `parse_proposal` (plain / inline_ship /
pull_request / malformed JSON).

## See also

- [CHANGELOG.md](./CHANGELOG.md)
- Reference implementation:
  `../vroxy_cuh/vroxy_dispatch/feedback_agent.py`
