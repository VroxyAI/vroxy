#!/usr/bin/env bash
#
# vroxy installer — the CLI, and optionally a dispatch agent.
#
#   ./install.sh              install the CLI, then offer a dispatch agent
#   ./install.sh --cli        install just the `vroxy` CLI
#   ./install.sh --dispatch   add a dispatch workspace (no CLI prompt)
#   ./install.sh --unattended add a workspace from the environment
#   ./install.sh --update     git pull + reinstall deps + restart all
#   ./install.sh --list       what's installed, and whether it's up
#   ./install.sh --remove ID  stop, disable and forget one instance
#   ./install.sh --migrate-legacy [ID]
#                             move a pre-template unit onto the
#                             template.  Needs the agent's EXACT name.
#
# --unattended reads VROXY_TOKEN (required), VROXY_HOST, CODE_ROOT,
# PROJECT, VROXY_AGENT_NAME, VROXY_INSTANCE_ID and DISPATCH_ENGINE.
# It exists for cloud-init, which cannot answer a prompt.
#
# ONE PROCESS PER WORKSPACE. AdminFeedbackChannel streams for exactly
# one tenant, so running dispatch for both vroxy and arubamu means two
# units, not one process with two connections. That's why this uses a
# systemd TEMPLATE unit (`vroxy-dispatch@<id>.service`) — adding the
# second workspace costs one env file, not a second copy of the unit.
#
# Each instance gets a generated VROXY_INSTALL_ID. The server keys the
# agent row on it, which is what makes several dispatches in ONE
# workspace possible too; without it the server can only tell them
# apart when there's exactly one.
#
# Nothing here writes a token to stdout, the log, or the process list.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
UNIT_NAME="vroxy-dispatch@.service"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
ENV_DIR="/etc/vroxy-dispatch"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "$RUN_USER")"

say()  { printf '%s\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

REPO_URL="${VROXY_REPO_URL:-https://github.com/VroxyAI/vroxy.git}"
CHECKOUT="${VROXY_HOME:-$HOME/.local/share/vroxy}"

# Piped from curl there is no script on disk, so BASH_SOURCE resolves
# to the caller's CWD and every "$HERE/..." path below would point at
# whatever directory they happened to be standing in.  Fetch a real
# checkout and re-exec inside it.
bootstrap_if_piped() {
  [[ -f "$HERE/feedback_agent.py" && -f "$HERE/pyproject.toml" ]] && return
  need git
  if [[ -d "$CHECKOUT/.git" ]]; then
    say "Updating $CHECKOUT…"
    git -C "$CHECKOUT" pull --ff-only --quiet || die "could not update $CHECKOUT"
  else
    say "Fetching vroxy into $CHECKOUT…"
    mkdir -p "$(dirname "$CHECKOUT")"
    git clone --quiet "$REPO_URL" "$CHECKOUT" || die "clone failed"
  fi
  # Reattach stdin to the terminal: piped, stdin IS the script, so the
  # dispatch prompt would never see an answer and would silently skip
  # the question this installer exists to ask.
  if { : < /dev/tty; } 2>/dev/null; then
    exec bash "$CHECKOUT/install.sh" "$@" < /dev/tty
  fi
  exec bash "$CHECKOUT/install.sh" "$@"
}

# ── venv ──────────────────────────────────────────────────────────
ensure_venv() {
  need python3
  if [[ ! -x "$HERE/.venv/bin/python" ]]; then
    say "Creating virtualenv…"
    python3 -m venv "$HERE/.venv"
  fi
  say "Installing Python dependencies…"
  # `python -m pip`, never the `pip` script: its shebang hardcodes the
  # venv's ORIGINAL absolute path, so a venv that was copied from
  # another checkout (or moved with the repo) has a pip that cannot
  # execute at all — "required file not found" pointing at a directory
  # that no longer exists. The module entry point has no such problem.
  "$HERE/.venv/bin/python" -m pip install --quiet --upgrade pip
  "$HERE/.venv/bin/python" -m pip install --quiet -r "$HERE/requirements.txt"
}

# ── the template unit ─────────────────────────────────────────────
# `%i` is the instance id, so one file serves every workspace.
install_unit() {
  say "Installing ${UNIT_NAME}…"
  sudo mkdir -p "$ENV_DIR"
  sudo chmod 750 "$ENV_DIR"
  sudo tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=vroxy dispatch — %i
Documentation=https://github.com/wartron/vroxy_dispatch
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${HERE}
EnvironmentFile=${ENV_DIR}/%i.env
ExecStart=${HERE}/.venv/bin/python ${HERE}/feedback_agent.py
Restart=on-failure
RestartSec=5
# Long enough for an in-flight run's SIGTERM spool to reach disk.
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
}

# ── verifying a token before we wire anything to it ───────────────
# The point is the workspace NAME. Installing an agent against the
# wrong token is the kind of mistake you find out about a week later,
# in someone else's rooms.
verify_token() {
  local host="$1" token="$2"
  need curl
  curl -fsS -m 15 -H "Authorization: Bearer ${token}" \
       -H "Accept: application/json" "${host%/}/api/v1/whoami" 2>/dev/null || true
}

json_field() { python3 -c 'import json,sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
cur = data
for part in sys.argv[1].split("."):
    cur = (cur or {}).get(part)
print("" if cur is None else cur)' "$1"; }

write_env_file() {
  local id="$1" name="$2" host="$3" token="$4" agent_name="$5" code_root="$6" project="$7"
  local env_file="${ENV_DIR}/${id}.env"

  # Generated once and never regenerated: it IS this instance's
  # identity on the server. Rewriting it would orphan the agent row,
  # its room, and its history — which is why an existing one is read
  # back rather than minted again. cloud-init re-runs on every boot.
  local install_id=""
  if sudo -n test -e "$env_file" 2>/dev/null; then
    install_id="$(sudo -n sed -n 's/^VROXY_INSTALL_ID=//p' "$env_file" | head -1)"
  fi
  [[ -n "$install_id" ]] || install_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"

  local cable="${host%/}/cable"
  cable="${cable/https:/wss:}"
  cable="${cable/http:/ws:}"

  sudo mkdir -p "$ENV_DIR"
  sudo tee "$env_file" >/dev/null <<ENVFILE
# vroxy_dispatch — ${name}
# Written by install.sh. Contains a workspace token: keep 0640.
VROXY_CABLE_URL=${cable}
VROXY_SERVICE_TOKEN=${token}
VROXY_INSTALL_ID=${install_id}
VROXY_AGENT_NAME=${agent_name}
VROXY_DISPATCH_UNIT=vroxy-dispatch@${id}.service
CODE_ROOT=${code_root}
PROJECT=${project}
LOG_FILE=${HERE}/log/dispatch-${id}.log
LOG_LEVEL=INFO
PYTHONUNBUFFERED=1
PATH=${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
ENVFILE
  [[ -z "${DISPATCH_ENGINE:-}" ]] \
    || echo "DISPATCH_ENGINE=${DISPATCH_ENGINE}" | sudo tee -a "$env_file" >/dev/null
  sudo chown root:"$RUN_GROUP" "$env_file"
  sudo chmod 640 "$env_file"
}

# No prompts, no confirmations, and no token on the command line —
# cloud-init has no terminal and argv is world-readable.
unattended_workspace() {
  ensure_venv
  install_unit

  local host="${VROXY_HOST:-https://vroxy.ai}"
  local token="${VROXY_TOKEN:-}"
  [[ -n "$token" ]] || die "VROXY_TOKEN is required for --unattended"

  local body name slug
  body="$(verify_token "$host" "$token")"
  [[ -n "$body" ]] || die "couldn't reach ${host}/api/v1/whoami, or the token was refused"
  name="$(printf '%s' "$body" | json_field workspace.name || true)"
  slug="$(printf '%s' "$body" | json_field workspace.slug || true)"
  [[ -n "$name" ]] || die "that response didn't name a workspace — is VROXY_HOST right?"

  local id="${VROXY_INSTANCE_ID:-${slug:-workspace}}"
  local code_root="${CODE_ROOT:-$(dirname "$HERE")}"
  local project="${PROJECT:-vroxy_web}"
  local agent_name="${VROXY_AGENT_NAME:-Dispatch}"

  mkdir -p "$code_root"
  write_env_file "$id" "$name" "$host" "$token" "$agent_name" "$code_root" "$project"

  sudo systemctl enable --now "vroxy-dispatch@${id}.service"
  sleep 2
  systemctl is-active --quiet "vroxy-dispatch@${id}.service" \
    && say "vroxy-dispatch@${id} running for ${name} as \"${agent_name}\"." \
    || die "vroxy-dispatch@${id} did not start — journalctl -u vroxy-dispatch@${id} -n 50"
}

add_workspace() {
  ensure_venv
  install_unit

  local host token slug name id code_root project agent_name body
  read -rp "vroxy host [https://vroxy.ai]: " host
  host="${host:-https://vroxy.ai}"

  # -s: a token must not land in the terminal scrollback or history.
  read -rsp "Workspace API token (platform:dispatch or full): " token; echo
  [[ -n "$token" ]] || die "a token is required"

  say "Checking the token…"
  body="$(verify_token "$host" "$token")"
  [[ -n "$body" ]] || die "couldn't reach ${host}/api/v1/whoami, or the token was refused"
  name="$(printf '%s' "$body" | json_field workspace.name || true)"
  slug="$(printf '%s' "$body" | json_field workspace.slug || true)"
  [[ -n "$name" ]] || die "that response didn't name a workspace — is the host right?"

  if [[ "$(printf '%s' "$body" | json_field dispatch_ok)" != "True" ]]; then
    warn "That token lacks platform:dispatch (or full) scope — the cable will refuse it."
    read -rp "Continue anyway? [y/N]: " go
    [[ "$go" == "y" || "$go" == "Y" ]] || exit 1
  fi

  say ""
  say "Workspace: ${name} (${slug})"
  read -rp "Install dispatch for this workspace? [Y/n]: " confirm
  [[ -z "$confirm" || "$confirm" == "y" || "$confirm" == "Y" ]] || exit 1

  read -rp "Code root (the folder holding the repos it works on) [$(dirname "$HERE")]: " code_root
  code_root="${code_root:-$(dirname "$HERE")}"
  [[ -d "$code_root" ]] || die "no such directory: $code_root"

  read -rp "Default project (repo folder name under that root) [vroxy_web]: " project
  project="${project:-vroxy_web}"

  read -rp "Agent name as it appears in the workspace [Dispatch]: " agent_name
  agent_name="${agent_name:-Dispatch}"

  id="${slug:-workspace}"
  local env_file="${ENV_DIR}/${id}.env"
  if [[ -e "$env_file" ]]; then
    read -rp "${id} is already installed — overwrite its config? [y/N]: " over
    [[ "$over" == "y" || "$over" == "Y" ]] || exit 1
  fi

  write_env_file "$id" "$name" "$host" "$token" "$agent_name" "$code_root" "$project"

  say "Starting vroxy-dispatch@${id}…"
  sudo systemctl enable --now "vroxy-dispatch@${id}.service"
  sleep 2
  systemctl is-active --quiet "vroxy-dispatch@${id}.service" \
    && say "Running. It registers itself as \"${agent_name}\" in ${name} on its first heartbeat." \
    || warn "Not running — journalctl -u vroxy-dispatch@${id} -n 50"
}

instances() {
  # `sudo`: ENV_DIR is 0750 root:root because the env files hold
  # workspace tokens, so an unprivileged `find` reads nothing and
  # --list cheerfully reported "nothing installed" over a running
  # instance. Listing names is not privileged; reading the files is.
  sudo -n test -d "$ENV_DIR" 2>/dev/null || return 0
  sudo -n find "$ENV_DIR" -maxdepth 1 -name '*.env' -printf '%f\n' 2>/dev/null | sed 's/\.env$//' | sort
}

LEGACY_UNIT="vroxy-dispatch-feedback-agent.service"
LEGACY_ENV="/etc/default/vroxy-dispatch"

# The pre-template unit, if this box still runs one.  It predates both
# the template and VROXY_INSTALL_ID, so `instances` cannot see it and
# --list reported a box as empty while an agent ran on it.
legacy_present() {
  systemctl list-unit-files "$LEGACY_UNIT" >/dev/null 2>&1 &&
    systemctl cat "$LEGACY_UNIT" >/dev/null 2>&1
}

list_instances() {
  local any=0
  while read -r id; do
    [[ -n "$id" ]] || continue
    any=1
    printf '%-20s %s\n' "$id" "$(systemctl is-active "vroxy-dispatch@${id}.service" 2>/dev/null || echo unknown)"
  done < <(instances)
  if legacy_present; then
    any=1
    printf '%-20s %s  (legacy unit — ./install.sh --migrate-legacy)\n' \
      "$LEGACY_UNIT" "$(systemctl is-active "$LEGACY_UNIT" 2>/dev/null || echo unknown)"
  fi
  [[ $any -eq 1 ]] || say "Nothing installed yet — run ./install.sh"
}

update_all() {
  say "Updating the checkout…"
  git -C "$HERE" pull --ff-only
  ensure_venv
  install_unit
  while read -r id; do
    [[ -n "$id" ]] || continue
    say "Restarting vroxy-dispatch@${id}…"
    # Out of band: a restart issued from inside the unit's own cgroup
    # takes this script down with it when systemd stops the unit.
    sudo systemd-run --on-active=2s --unit="vroxy-dispatch-update-${id}-$RANDOM" --collect \
      systemctl restart "vroxy-dispatch@${id}.service"
  done < <(instances)
  if legacy_present; then
    say "Restarting ${LEGACY_UNIT}…"
    sudo systemd-run --on-active=2s --unit="vroxy-dispatch-update-legacy-$RANDOM" --collect \
      systemctl restart "$LEGACY_UNIT"
  fi
  say "Restarts scheduled. Each instance spools its in-flight work and replays it on boot."
}

remove_instance() {
  local id="$1"
  [[ -n "$id" ]] || die "usage: ./install.sh --remove <id>"
  sudo systemctl disable --now "vroxy-dispatch@${id}.service" 2>/dev/null || true
  sudo rm -f "${ENV_DIR}/${id}.env"
  say "Removed ${id}. The agent row and its room stay in the workspace — delete them there if you want them gone."
}

# ── the CLI ───────────────────────────────────────────────────────
# Installed for the invoking user, never system-wide: `vroxy` is an
# ordinary command and has no business needing root.  pipx when it is
# there (its own venv, no clashes), pip --user otherwise.
install_cli() {
  need python3
  if command -v pipx >/dev/null 2>&1; then
    pipx install --force "$HERE" >/dev/null || die "pipx install failed"
  elif python3 -m pip install --user --quiet --upgrade "$HERE" 2>/dev/null; then
    :
  else
    # PEP 668: a distro-managed python refuses --user installs.  Own a
    # venv rather than arguing with it, and put the entry point on the
    # PATH by hand.  No root, nothing outside $HOME.
    say "System python is externally managed — installing the CLI into its own venv."
    if [[ ! -x "$HERE/.venv-cli/bin/python" ]]; then
      python3 -m venv "$HERE/.venv-cli" || {
        rm -rf "$HERE/.venv-cli"
        die "could not create a virtualenv — on Debian/Ubuntu: sudo apt install python3-venv"
      }
    fi
    "$HERE/.venv-cli/bin/python" -m pip install --quiet --upgrade "$HERE" ||
      die "venv install failed"
    mkdir -p "$HOME/.local/bin"
    ln -sf "$HERE/.venv-cli/bin/vroxy" "$HOME/.local/bin/vroxy"
  fi
  say "Installed the vroxy CLI.  Try: vroxy --help"
  command -v vroxy >/dev/null 2>&1 ||
    warn "vroxy is not on PATH yet — add ~/.local/bin to PATH, or restart your shell."
}

# The default install: everyone wants the CLI, only some people want a
# long-running agent on this box.  Asking beats assuming — the daemon
# writes systemd units and an env file, which is not what someone who
# typed `curl | bash` for a command expects.
default_install() {
  install_cli
  if [[ ! -t 0 ]]; then
    say "Not a terminal — skipping the dispatch agent.  Run ./install.sh --dispatch to add one."
    return
  fi
  printf '\nAlso run a dispatch agent on this machine? It connects to one\nworkspace and installs a systemd unit. [y/N] '
  local answer
  read -r answer || answer=""
  case "$answer" in
    [yY]*) add_workspace ;;
    *)     say "Skipped.  Run ./install.sh --dispatch later if you change your mind." ;;
  esac
}

migrate_legacy() {
  legacy_present || die "no legacy unit on this box — nothing to migrate."
  need python3

  local id="${1:-}"
  [[ -n "$id" ]] || { printf 'Instance id for this workspace (e.g. vroxy): '; read -r id; }
  [[ -n "$id" ]] || die "an instance id is required."
  sudo -n test -e "${ENV_DIR}/${id}.env" 2>/dev/null &&
    die "${ENV_DIR}/${id}.env already exists — pick another id or remove that instance first."

  local legacy
  legacy="$(sudo cat "$LEGACY_ENV" 2>/dev/null)" || die "cannot read $LEGACY_ENV"
  local host token agent_name code_root project cable
  cable="$(sed -n 's/^VROXY_CABLE_URL=//p'    <<<"$legacy" | head -1)"
  token="$(sed -n 's/^VROXY_SERVICE_TOKEN=//p' <<<"$legacy" | head -1)"
  agent_name="$(sed -n 's/^VROXY_AGENT_NAME=//p' <<<"$legacy" | head -1)"
  code_root="$(sed -n 's/^CODE_ROOT=//p'       <<<"$legacy" | head -1)"
  project="$(sed -n 's/^PROJECT=//p'           <<<"$legacy" | head -1)"
  [[ -n "$token" ]] || die "no VROXY_SERVICE_TOKEN in $LEGACY_ENV"

  host="${cable%/cable}"
  host="${host/wss:/https:}"
  host="${host/ws:/http:}"

  # THE ONE THING THIS CANNOT GUESS.  The legacy unit predates
  # VROXY_INSTALL_ID, so the server matched it by "the workspace's only
  # local agent".  Once it sends an install id it is resolved by id,
  # and the server ADOPTS the existing row only when the name matches
  # exactly (DispatchAgent.adoptable).  A blank or wrong name creates a
  # SECOND agent and strands the original's room and history.
  if [[ -z "$agent_name" ]]; then
    say ""
    say "This agent has no name in $LEGACY_ENV."
    say "Open /w/<workspace>/dispatch and copy the agent's name EXACTLY."
    say "Get it wrong and the server makes a second agent instead of"
    say "adopting this one — its room and history stay on the old row."
    printf 'Agent name: '
    read -r agent_name
  fi
  [[ -n "$agent_name" ]] || die "an agent name is required — see /w/<workspace>/dispatch"

  say "Migrating ${LEGACY_UNIT} → vroxy-dispatch@${id}.service (agent: ${agent_name})"
  write_env_file "$id" "$id" "$host" "$token" "$agent_name" "$code_root" "$project"
  install_unit
  sudo systemctl enable "vroxy-dispatch@${id}.service" >/dev/null

  # Out of band: this script may itself be running inside the unit it
  # is about to stop.
  sudo systemd-run --on-active=2s --unit="vroxy-dispatch-migrate-$RANDOM" --collect \
    bash -c "systemctl disable --now ${LEGACY_UNIT}; systemctl start vroxy-dispatch@${id}.service"
  say "Scheduled. The old unit stops and the new one starts in ~2s;"
  say "in-flight work is spooled and replayed. Check: ./install.sh --list"
}

bootstrap_if_piped "$@"

case "${1:-}" in
  --unattended) unattended_workspace ;;
  --cli)        install_cli ;;
  --dispatch)   add_workspace ;;
  --migrate-legacy) migrate_legacy "${2:-}" ;;
  --update) update_all ;;
  --list)   list_instances ;;
  --remove) remove_instance "${2:-}" ;;
  --help|-h)
    sed -n '2,20p' "$HERE/install.sh" | sed 's/^# \{0,1\}//'
    ;;
  "")       default_install ;;
  *)        die "unknown option: $1 (try --help)" ;;
esac
