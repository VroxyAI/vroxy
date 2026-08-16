# ctovibe_dispatch

An ActionCable client that lets an admin (or a widget visitor's
`/note` slash command) pass messages to **Claude Code (headless)**
and stream the response back into ctovibe's support chat.

Ported from
[`vroxy_dispatch/feedback_agent.py`](https://github.com/wartron/vroxy_web/tree/master/vroxy_dispatch)
with adjustments for ctovibe's tenant-scoped channel + hashid
message identifiers.

## Runtime shape

1. Connects `wss://ctovibe.ai/cable?token=<CTOVIBE_SERVICE_TOKEN>`.
2. Subscribes `{ channel: "AdminFeedbackChannel" }`. Rails resolves
   the token → `Tenant`-owned `ApiToken` → sets
   `current_tenant`, and the channel streams for exactly that
   tenant.
3. On `feedback.created` (fanned out by
   `POST /widget/feedback`) → builds a prompt → runs `claude-chat`
   (or streamed `claude -p --output-format stream-json` for live
   tool chips) → parses an optional fenced ` ```proposal ``` `
   block → replies over the socket.
4. On `approve.requested` (operator clicked Ship-it) → writes the
   proposal files, `git commit`, `git push` (auto-deploys) — or
   opens a PR via `gh pr create` for `mode: pull_request`.
5. Heartbeat every 20 s; one in-flight worker at a time
   (Claude sessions aren't reentrant).

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
token  = tenant.api_tokens.new(name: "ctovibe_dispatch",
                                scopes: ["platform:dispatch"])
token.generate!
token.save!
puts token.raw_token  # ← ONLY chance to see it; store in env immediately
```

The `raw_token` is 48 chars of `SecureRandom.alphanumeric`. Copy it
into the dispatch host's environment as `CTOVIBE_SERVICE_TOKEN`.
Rotate by `token.revoke!` and creating a new one.

## Run (foreground)

```bash
CTOVIBE_SERVICE_TOKEN=<token>  .venv/bin/python feedback_agent.py
```

`Subscribed to AdminFeedbackChannel` in the log means dispatch is
ready. `reject_subscription` means the token is wrong scope or
attached to a User instead of a Tenant.

### Environment

| Var                     | Default                             |
| ----------------------- | ----------------------------------- |
| `CTOVIBE_CABLE_URL`     | `wss://ctovibe.ai/cable`            |
| `CTOVIBE_SERVICE_TOKEN` | *(required)*                        |
| `CODE_ROOT`             | parent of this checkout             |
| `PROJECT`               | `ctovibe_web`                       |
| `CLAUDE_CHAT_BIN`       | `./bin/claude-chat`                 |
| `CLAUDE_STREAM`         | `1` (set `0` to skip streamed path) |
| `LOG_LEVEL`             | `INFO`                              |

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
CTOVIBE_CABLE_URL=ws://localhost:3002/cable \
CTOVIBE_SERVICE_TOKEN=4VK25VuTFzU830vhHG8AybL1v7Yfmjdgt5n5FcPMUInZxWkD \
PYTHONUNBUFFERED=1 \
python3 feedback_agent.py
```

`CODE_ROOT` / `PROJECT` need no override — they already default to
the sibling `ctovibe_web` checkout. `python3` works without the venv
if your system Python already satisfies `requirements.txt`
(`websockets>=12,<14`); use `.venv/bin/python` otherwise.

### The local dispatch token

`db/seeds/ctovibe_tenant.rb` seeds the `ctovibe` tenant
(`public_key: w8eYmQ8uPppj2BgEBywUSaoz`) with a `seed dashboard
token`, but that one is scoped `tenant:read tenant:write` —
`AdminFeedbackChannel#subscribed` rejects it. Dispatch needs `full`
or `platform:dispatch`, so mint a second token:

```bash
docker compose exec web bin/rails runner '
t = Tenant.find_by!(slug: "ctovibe")
t.api_tokens.where(name: "ctovibe_dispatch dev").where(revoked_at: nil).find_each(&:revoke!)
puts ApiToken.generate!(owner: t, name: "ctovibe_dispatch dev",
                        scopes: ["platform:dispatch"]).raw_token'
```

The raw token is `SecureRandom.alphanumeric` — the value baked into
the command above is this machine's, and a `db:reset` (or a fresh
checkout) invalidates it. Re-run the minting command and paste the
new value in. Revoke when you're done:

```bash
docker compose exec web bin/rails runner \
  'ApiToken.find_by(name: "ctovibe_dispatch dev")&.revoke!'
```

The seeded tenant's `origin_allowlist` is empty, so
`Connection#origin_ok?` short-circuits true and `http://localhost:3002`
passes. If you populate the allowlist for widget testing, add that
origin or dispatch starts getting rejected at connect.

### Local dev doesn't sandbox the ship path

Pointing `CTOVIBE_CABLE_URL` at localhost only redirects the cable.
An approved proposal still runs a real `git commit` + `git push` in
`CODE_ROOT/PROJECT` (`handle_approve`), and `mode: pull_request`
still shells out to `gh pr create` against the real remote. Local
dev is a safe place to exercise `feedback.created` → `reply`; it is
not a safe place to click Ship-it unless you mean it.

## Run (systemd, recommended)

`/etc/default/ctovibe-dispatch`:

```ini
CTOVIBE_CABLE_URL=wss://ctovibe.ai/cable
CTOVIBE_SERVICE_TOKEN=REPLACE_ME

CODE_ROOT=/home/ubuntu/code/ctovibe
PROJECT=ctovibe_web

LOG_LEVEL=INFO
PYTHONUNBUFFERED=1
```

`/etc/systemd/system/ctovibe-dispatch.service`:

```ini
[Unit]
Description=ctovibe dispatch — AdminFeedbackChannel cable subscriber
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/code/ctovibe/ctovibe_dispatch
EnvironmentFile=/etc/default/ctovibe-dispatch
ExecStart=/home/ubuntu/code/ctovibe/ctovibe_dispatch/.venv/bin/python /home/ubuntu/code/ctovibe/ctovibe_dispatch/feedback_agent.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ctovibe-dispatch
sudo journalctl -u ctovibe-dispatch -f
```

## One tenant per process

Ctovibe's `AdminFeedbackChannel` streams for exactly one tenant
(the one the auth token belongs to). Multi-tenant hosting means
running one systemd unit per tenant (e.g.
`ctovibe-dispatch-<tenant_slug>.service`) with a per-tenant
`CTOVIBE_SERVICE_TOKEN` in the `EnvironmentFile`. See the vroxy
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
- **`approve.requested`** — operator clicked Ship-it on a
  proposal card. Dispatch writes files + commits + pushes.

### Dispatch → server

Dispatch calls these `action`s on the channel:

| Action    | Payload                                     | Server does                                                                          |
| --------- | ------------------------------------------- | ------------------------------------------------------------------------------------ |
| heartbeat | `version`, `meta`                           | `Rails.cache.write("ctovibe_dispatch:heartbeat:<tenant.id>", {...}, expires_in: 60)` |
| progress  | `chat_id`, `name`, `input`                  | Persists a `tool_call` `SupportChatMessage` + broadcasts a `tool` chip on `SupportChatChannel` |
| reply     | `chat_id`, `body`, `kind` (opt), `proposal` (opt) | Persists an assistant `SupportChatMessage` + broadcasts `done` on `SupportChatChannel`; bumps linked feedback `open → triaged` |

## Feedback source

Feedback lands via `POST /widget/feedback`
([`app/controllers/widget/feedbacks_controller.rb`](../ctovibe_web/app/controllers/widget/feedbacks_controller.rb))
which creates the `AdminUiFeedback` + a linked `SupportChat` and
calls `AdminFeedbackChannel.broadcast_feedback_created`.

The client-side picker that populates the payload lives in
`ctovibe_web/app/javascript/admin_ui_inspector.js`.

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
