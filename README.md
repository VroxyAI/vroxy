# vroxy_cli

Command-line client for a vroxy workspace. Python 3.9+, **stdlib
only** — the install path is `curl | bash` onto a box that may have
nothing but `python3`.

```bash
vroxy login                      # prompts; token saved 0600
vroxy workspaces
vroxy rooms <workspace>
vroxy read <workspace> <room> --limit 50
vroxy post <workspace> <room> "shipped 2.147.3"
echo "from a pipe" | vroxy post <workspace> <room> -
vroxy dispatch <workspace>       # is the agent up
```

`--json` on any command gives raw JSON, which is the point: this
exists so an agent in a checkout can answer questions about live
workspace state instead of asking a human to copy-paste. See
[#91](https://github.com/wartron/vroxy_web/issues/91).

## It is a client, not a new permission surface

Everything goes through `/api/mobile/v1`, which is only called
"mobile" because the phone shipped first. It is the operator API: the
workspace comes from `/workspaces/:hashid/...` resolved through the
caller's own memberships, and every action is gated on their
capability. So the CLI can do exactly what the person holding the
token can do in the web app, no more — and revoking is removing a
seat.

## The token

`~/.config/vroxy/credentials.json`, mode **0600**, set before the
token is written rather than after — a token written first and
chmod'd second is world-readable for the gap between. A token saved
for one host is never sent to another.

The password is never a command-line argument. `argv` is readable
through `/proc` and lands in shell history; `login` prompts, or reads
`VROXY_PASSWORD` from the environment for CI.

Overrides: `VROXY_HOST`, `VROXY_TOKEN`, `VROXY_CONFIG_DIR`.

## Known gap

**Room webhook URLs are not in any API** — the question that prompted
#91 ("what is this room's ingest URL") still cannot be answered over
HTTP. That needs a read endpoint in `vroxy_web` listing a room's
webhook keys by name, prefix and active state. It must not return the
raw token: only a digest is stored, the path token IS the credential,
and minting stays in the UI.

## Tests

```bash
python3 -m unittest discover -s tests
```

No network. The two that matter assert the credentials file is 0600
and that a token is not reused across hosts; both were verified to
fail when the guard is removed.
