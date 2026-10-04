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
#   ./install.sh --doctor     check every env file for misconfig and print fixes
#   ./install.sh --remove ID  stop, disable and forget one instance
#   ./install.sh --migrate-legacy [ID]
#                             move a pre-template unit onto the
#                             template.  Needs the agent's EXACT name.
#
# --unattended reads VROXY_TOKEN (required), VROXY_HOST, CODE_ROOT,
# PROJECT, VROXY_AGENT_NAME, VROXY_INSTANCE_ID and optional DISPATCH_ENGINE
# (seeds ~/.cache/vroxy-dispatch/engine-<id>; /harness owns it after that).
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

# A terminal with bracketed paste on wraps a paste in ESC[200~ … ESC[201~;
# bash's `read` builtin doesn't unwrap that, so the value lands in the
# variable as raw escape sequences and every prompt after the first looks
# broken. Disable it for the read (and strip the markers as a backstop).
read_prompt() {
  local var="$1" prompt="$2" mode="${3:-}" rc=0
  [[ -t 0 ]] && printf '\e[?2004l' >&2
  if [[ "$mode" == silent ]]; then
    if ! read -rsp "$prompt" "$var"; then rc=$?; fi
    printf '\n' >&2
  elif [[ "$mode" == masked ]]; then
    read_masked "$var" "$prompt"; rc=$?
  else
    if ! read -rp "$prompt" "$var"; then rc=$?; fi
  fi
  [[ -t 0 ]] && printf '\e[?2004h' >&2
  local val="${!var}"
  val="${val//$'\e'\[200~/}"
  val="${val//$'\e'\[201~/}"
  printf -v "$var" '%s' "$val"
  return "$rc"
}

# A token must not land in scrollback or history, but `read -s` shows
# nothing — a pasted key looks like it never took, so people paste it
# twice and the doubled token gets refused. Echo `*` per keystroke so
# the paste is visibly received without revealing the value.
read_masked() {
  local var="$1" prompt="$2"
  local pw="" char
  printf '%s' "$prompt" >&2
  while IFS= read -r -s -n1 char; do
    [[ -z "$char" ]] && { printf '\n' >&2; break; }
    if [[ "$char" == $'\177' || "$char" == $'\b' ]]; then
      [[ -n "$pw" ]] && { pw="${pw%?}"; printf '\b \b' >&2; }
    else
      pw+="$char"
      printf '*' >&2
    fi
  done
  printf -v "$var" '%s' "$pw"
}

# `read -s` leaves echo off if the script is interrupted mid-read; put the
# terminal back to a sane state however the script exits.
restore_tty() {
  [[ -t 0 ]] || return 0
  command -v stty >/dev/null 2>&1 && stty echo 2>/dev/null
  printf '\e[?2004h' >&2
}
trap restore_tty EXIT

# True when a code_root + project resolve to a real directory — the folder
# the agent will work in. False when the operator pointed code_root INTO
# the repo (code_root=/a/repo + project=repo → /a/repo/repo), which is the
# "CODE_ROOT/… not found" crash the agent hits later in the room.
repo_resolves() { [[ -d "${1%/}/${2}" ]]; }
is_git_repo()   { [[ -e "${1%/}/.git" ]]; }
sanitize_instance_id() { printf '%s\n' "${1//[^a-zA-Z0-9._-]/-}"; }

# The subdirectories of a code root — the folders an agent might work in.
dir_candidates() {
  local root="$1" d
  for d in "$root"/*/; do
    [[ -d "$d" ]] || continue
    basename "$d"
  done | sort
}

# Pick the default project by looking at what is actually under the code
# root instead of guessing a name. A monorepo (several folders) gets a
# numbered list; one folder becomes the default; none falls back to a
# free-form name. Writes the choice into the caller's variable by name.
choose_project() {
  local out="$1" root="$2"
  local dirs=() name i
  while IFS= read -r name; do
    [[ -n "$name" ]] && dirs+=("$name")
  done < <(dir_candidates "$root")

  if [[ ${#dirs[@]} -eq 0 ]]; then
    read_prompt name "Project folder (no folders found under $root): "
    printf -v "$out" '%s' "$name"
    return
  fi
  if [[ ${#dirs[@]} -eq 1 ]]; then
    read_prompt name "Default project [${dirs[0]}]: "
    printf -v "$out" '%s' "${name:-${dirs[0]}}"
    return
  fi

  say "Found ${#dirs[@]} folders under $root:"
  for i in "${!dirs[@]}"; do say "    $((i + 1)). ${dirs[$i]}"; done
  read_prompt name "Default project — number or folder name [1]: "
  name="${name:-1}"
  if [[ "$name" =~ ^[0-9]+$ ]] && (( name >= 1 && name <= ${#dirs[@]} )); then
    printf -v "$out" '%s' "${dirs[$((name - 1))]}"
  else
    printf -v "$out" '%s' "$name"
  fi
}

# Env files live under a root:root 0750 dir, so reading one is a `sudo`.
# `env_field` pulls one KEY=value out of a file's CONTENTS (a string, not a
# path), so the parse is testable without touching /etc.
read_env() { sudo -n cat "${ENV_DIR}/${1}.env" 2>/dev/null || true; }
env_field() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -1; }

# The coding CLIs vroxy_dispatch can drive, with a one-line install. Keep
# in step with feedback_agent.py's KNOWN_HARNESSES (the engine-bearing
# entries); the bin names here mirror `_resolve_harness_bin`'s guesses.
HARNESS_SUGGESTIONS=(
  "claude|Claude Code|npm install -g @anthropic-ai/claude-code"
  "codex|OpenAI Codex|npm install -g @openai/codex"
  "opencode|OpenCode|curl -fsSL https://opencode.ai/install | bash"
  "gemini|Gemini CLI|npm install -g @google/gemini-cli"
  "copilot|GitHub Copilot CLI|npm install -g @github/copilot"
  "cursor-agent|Cursor Agent|npm install -g cursor-agent"
)

harness_bin_installed() {
  local bin="$1"
  command -v "$bin" >/dev/null 2>&1 && return 0
  local guess
  for guess in "$HOME/.local/bin/$bin" "$HOME/.$bin/bin/$bin" \
               "$HOME/bin/$bin" "/usr/local/bin/$bin"; do
    [[ -x "$guess" ]] && return 0
  done
  return 1
}

# After an install, say which harnesses are missing so a box with none of
# them doesn't sit there silently refusing to answer.
suggest_harnesses() {
  local id="$1" found=0 line bin label cmd npm_missing=0
  for line in "${HARNESS_SUGGESTIONS[@]}"; do
    IFS='|' read -r bin label cmd <<<"$line"
    harness_bin_installed "$bin" && found=$((found + 1))
  done
  [[ $found -gt 0 ]] && return 0
  npm_present || npm_missing=1
  say ""
  say "No coding CLI found on this machine. vroxy_dispatch drives one of these"
  say "to do the work — install at least one (the default engine is claude):"
  for line in "${HARNESS_SUGGESTIONS[@]}"; do
    IFS='|' read -r bin label cmd <<<"$line"
    say "    ${label} — ${cmd}"
  done
  if [[ $npm_missing -eq 1 ]]; then
    say "(npm is not installed — run: $(node_setup_hint), or use opencode above, which needs no npm)"
  fi
  say "Then flip the harness in a dispatch room (/harness cursor), or seed"
  say "~/.cache/vroxy-dispatch/engine-${id} with the engine name and restart:"
  say "  sudo systemctl restart vroxy-dispatch@${id}.service"
}

python_setup_hint() {
  local ver
  ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3)"
  if command -v apt-get >/dev/null 2>&1; then
    printf 'sudo apt install python3 python3-venv python3-pip python%s-venv' "$ver"
  elif command -v dnf >/dev/null 2>&1; then
    printf 'sudo dnf install python3 python3-pip'
  elif command -v apk >/dev/null 2>&1; then
    printf 'sudo apk add python3 py3-pip py3-virtualenv'
  elif command -v brew >/dev/null 2>&1; then
    printf 'brew install python'
  else
    printf "install your distro's python3 venv and pip packages"
  fi
}

# Root, or passwordless sudo: we can install the packages ourselves
# rather than dying with a hint the operator has to copy-paste.
can_install_packages() {
  if [[ "$(id -u)" -eq 0 ]]; then
    return 0
  fi
  command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null
}

run_pkg() {
  if [[ "$(id -u)" -eq 0 ]]; then
    env DEBIAN_FRONTEND=noninteractive "$@"
  else
    sudo DEBIAN_FRONTEND=noninteractive "$@"
  fi
}

apt_install_python_pkgs() {
  local ver pkgs
  say "Installing the Python packages this installer needs…"
  run_pkg apt-get update -qq || die "apt-get update failed"
  pkgs=(python3 python3-venv python3-pip)
  run_pkg apt-get install -y -qq "${pkgs[@]}" ||
    die "could not install ${pkgs[*]}. Fix it with: $(python_setup_hint)"
  ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [[ -n "$ver" ]]; then
    run_pkg apt-get install -y -qq "python${ver}-venv" 2>/dev/null || true
  fi
}

npm_present() { command -v npm >/dev/null 2>&1; }

node_setup_hint() {
  if command -v apt-get >/dev/null 2>&1; then
    printf 'sudo apt install -y nodejs npm'
  elif command -v dnf >/dev/null 2>&1; then
    printf 'sudo dnf install -y nodejs npm'
  else
    printf 'install Node.js + npm for your distro (or use opencode, which needs no npm)'
  fi
}

# Most coding CLIs are npm packages. A box without npm would be told
# `npm install -g …` and then fail with "command not found", so offer to
# install node + npm first (default Yes) — the same as the Python
# toolchain this installer already provisions. opencode is the npm-free
# escape hatch and the message says so.
ensure_node() {
  npm_present && return 0
  if [[ "${VROXY_INSTALL_NO_APT:-}" != "1" ]] &&
     command -v apt-get >/dev/null 2>&1 && can_install_packages; then
    read_prompt go "npm is not installed — install nodejs + npm now? [Y/n]: "
    [[ -z "$go" || "$go" == "y" || "$go" == "Y" ]] || return 1
    say "Installing nodejs + npm…"
    run_pkg apt-get update -qq || true
    run_pkg apt-get install -y -qq nodejs npm ||
      { warn "couldn't install nodejs/npm — $(node_setup_hint)"; return 1; }
    npm_present && return 0
  fi
  warn "npm is not installed — most coding CLIs need it (opencode doesn't)."
  warn "Install it with: $(node_setup_hint)"
  return 1
}

# Distros that ship python3 without ensurepip leave a half-made
# venv behind; probe in a temp dir so we never trust that debris.
python_venv_works() {
  command -v python3 >/dev/null 2>&1 || return 1
  local probe
  probe="$(mktemp -d "${TMPDIR:-/tmp}/vroxy-venv-probe.XXXXXX")"
  if python3 -m venv "$probe" >/dev/null 2>&1 && venv_usable "$probe"; then
    rm -rf "$probe"
    return 0
  fi
  rm -rf "$probe"
  return 1
}

# Install python3 + venv/pip via apt when we can (root or passwordless
# sudo). VROXY_INSTALL_NO_APT=1 keeps unit tests from touching apt.
ensure_python_toolchain() {
  if python_venv_works; then
    return 0
  fi
  if [[ "${VROXY_INSTALL_NO_APT:-}" != "1" ]] &&
     command -v apt-get >/dev/null 2>&1 && can_install_packages; then
    apt_install_python_pkgs
    python_venv_works && return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    die "missing required command: python3. Fix it with: $(python_setup_hint) — then run ./install.sh again."
  fi
  die "python3 can't create a virtualenv with pip on this machine. Fix it with: $(python_setup_hint) — then run ./install.sh again."
}

venv_usable() { [[ -x "$1/bin/python" ]] && "$1/bin/python" -m pip --version >/dev/null 2>&1; }

make_venv() {
  local dir="$1"
  venv_usable "$dir" && return 0
  [[ -e "$dir" ]] && say "Replacing an incomplete virtualenv at $dir…"
  rm -rf "$dir"
  say "Creating virtualenv…"
  if ! python3 -m venv "$dir" >/dev/null 2>&1 || ! venv_usable "$dir"; then
    rm -rf "$dir"
    die "python3 can't create a virtualenv with pip on this machine. Fix it with: $(python_setup_hint) — then run ./install.sh again."
  fi
}

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
  ensure_python_toolchain
  make_venv "$HERE/.venv"
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

# The workspace's existing agents from a `whoami` body, one per line as
# `name\tready|offline` (empty when none). Lets an operator see what is
# already registered before naming another, and avoid a silent rename.
whoami_agents() { python3 -c 'import json,sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for a in (data or {}).get("agents") or []:
    if isinstance(a, dict) and a.get("name"):
        status = "ready" if a.get("online") else "offline"
        print(a["name"] + "\t" + status)'; }

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
# DISPATCH_AUTO_UPDATE=0
ENVFILE
  sudo chown root:"$RUN_GROUP" "$env_file"
  sudo chmod 640 "$env_file"
  # Harness preference is user-writable state, not the root-owned
  # EnvironmentFile — /harness flips it without sudo.
  if [[ -n "${DISPATCH_ENGINE:-}" ]]; then
    local state_dir="${XDG_CACHE_HOME:-$HOME/.cache}/vroxy-dispatch"
    mkdir -p "$state_dir"
    printf '%s\n' "$DISPATCH_ENGINE" > "${state_dir}/engine-${id}"
  fi
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

  local host token slug name id code_root project agent_name body agents_list agent status
  read_prompt host "vroxy host [https://vroxy.ai]: "
  host="${host:-https://vroxy.ai}"

  read_prompt token "Workspace API token (platform:dispatch or full): " masked
  [[ -n "$token" ]] || die "a token is required"

  say "Checking the token…"
  body="$(verify_token "$host" "$token")"
  [[ -n "$body" ]] || die "couldn't reach ${host}/api/v1/whoami, or the token was refused"
  name="$(printf '%s' "$body" | json_field workspace.name || true)"
  slug="$(printf '%s' "$body" | json_field workspace.slug || true)"
  [[ -n "$name" ]] || die "that response didn't name a workspace — is the host right?"

  if [[ "$(printf '%s' "$body" | json_field dispatch_ok)" != "True" ]]; then
    warn "That token lacks platform:dispatch (or full) scope — the cable will refuse it."
    read_prompt go "Continue anyway? [y/N]: "
    [[ "$go" == "y" || "$go" == "Y" ]] || exit 1
  fi

  say ""
  say "Workspace: ${name} (${slug})"
  agents_list="$(printf '%s' "$body" | whoami_agents)"
  if [[ -n "$agents_list" ]]; then
    say "Already registered in this workspace:"
    while IFS=$'\t' read -r agent status; do
      [[ -n "$agent" ]] || continue
      [[ "$status" == "ready" ]] && say "    • ${agent} (connected)" || say "    • ${agent} (not connected)"
    done <<< "$agents_list"
  fi
  read_prompt confirm "Install dispatch for this workspace? [Y/n]: "
  [[ -z "$confirm" || "$confirm" == "y" || "$confirm" == "Y" ]] || exit 1

  read_prompt code_root "Code root (the PARENT folder that holds your repos) [$(dirname "$HERE")]: "
  code_root="${code_root:-$(dirname "$HERE")}"
  [[ -d "$code_root" ]] || die "no such directory: $code_root"

  if is_git_repo "$code_root"; then
    project="$(basename "$code_root")"
    code_root="$(dirname "$code_root")"
    say "Single project — CODE_ROOT=$code_root, PROJECT=$project"
  else
    choose_project project "$code_root"
    if [[ -z "$project" ]]; then
      warn "No project folder — CODE_ROOT is the parent folder and PROJECT"
      warn "is the repo under it, e.g. $code_root/<project>."
      read_prompt go "Continue anyway? [y/N]: "
      [[ "$go" == "y" || "$go" == "Y" ]] || exit 1
    fi
  fi

  read_prompt agent_name "Agent name as it appears in the workspace [Dispatch]: "
  agent_name="${agent_name:-Dispatch}"
  if printf '%s' "$agents_list" | cut -f1 | grep -Fqx "$agent_name"; then
    warn "\"${agent_name}\" is already registered in this workspace — the server will rename this one to keep it distinct."
  fi

  id="${slug:-workspace}"
  local env_file="${ENV_DIR}/${id}.env"
  if [[ -e "$env_file" ]]; then
    say "${id} is already installed on this machine."
    read_prompt another "Add a second agent for this workspace instead? [y/N]: "
    if [[ "$another" == "y" || "$another" == "Y" ]]; then
      id="${slug}-$(sanitize_instance_id "$agent_name")"
      env_file="${ENV_DIR}/${id}.env"
      while [[ -e "$env_file" ]]; do
        read_prompt id "Instance id for this one (e.g. ${slug}-frontend): "
        [[ -n "$id" ]] || exit 1
        env_file="${ENV_DIR}/${id}.env"
      done
    else
      read_prompt over "Overwrite the existing ${slug} config? [y/N]: "
      [[ "$over" == "y" || "$over" == "Y" ]] || exit 1
    fi
  fi

  write_env_file "$id" "$name" "$host" "$token" "$agent_name" "$code_root" "$project"

  say "Starting vroxy-dispatch@${id}…"
  sudo systemctl enable --now "vroxy-dispatch@${id}.service"
  sleep 2
  systemctl is-active --quiet "vroxy-dispatch@${id}.service" \
    && say "Running. It registers itself as \"${agent_name}\" in ${name} on its first heartbeat." \
    || warn "Not running — journalctl -u vroxy-dispatch@${id} -n 50"
  say "Logs: ${HERE}/log/dispatch-${id}.log (tail -f) or journalctl -u vroxy-dispatch@${id} -f"
  ensure_node
  suggest_harnesses "$id"
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
    local contents name project
    contents="$(read_env "$id")"
    name="$(env_field "$contents" VROXY_AGENT_NAME)"
    project="$(env_field "$contents" PROJECT)"
    printf '%-20s %-9s agent=%-16s project=%s\n' \
      "$id" "$(systemctl is-active "vroxy-dispatch@${id}.service" 2>/dev/null || echo unknown)" \
      "${name:-?}" "${project:-?}"
  done < <(instances)
  if legacy_present; then
    any=1
    printf '%-20s %-9s %s\n' \
      "$LEGACY_UNIT" "$(systemctl is-active "$LEGACY_UNIT" 2>/dev/null || echo unknown)" \
      "(legacy unit — ./install.sh --migrate-legacy)"
  fi
  [[ $any -eq 1 ]] || say "Nothing installed yet — run ./install.sh"
}

# A health check over every env file on the box: the misconfigurations an
# agent can carry that only show up later — a shared install id (two agents,
# one room), a CODE_ROOT/PROJECT that doesn't resolve, a unit name that
# points at another instance — each with the exact fix. Exits non-zero when
# anything is wrong so a script can gate on it.
doctor() {
  if ! sudo -n true 2>/dev/null; then
    warn "cannot read ${ENV_DIR} without passwordless sudo — run with sudo, or grant it."
    return 2
  fi

  local ids=() id
  while read -r id; do
    [[ -n "$id" ]] && ids+=("$id")
  done < <(instances)

  if [[ ${#ids[@]} -eq 0 ]]; then
    say "Nothing installed yet — run ./install.sh"
    return 0
  fi

  local problems=0
  declare -A install_ids=()
  declare -A agent_names=()

  say "Checking ${#ids[@]} instance(s)…"
  for id in "${ids[@]}"; do
    local contents token install_id agent_name unit code_root project
    contents="$(read_env "$id")"
    token="$(env_field "$contents" VROXY_SERVICE_TOKEN)"
    install_id="$(env_field "$contents" VROXY_INSTALL_ID)"
    agent_name="$(env_field "$contents" VROXY_AGENT_NAME)"
    unit="$(env_field "$contents" VROXY_DISPATCH_UNIT)"
    code_root="$(env_field "$contents" CODE_ROOT)"
    project="$(env_field "$contents" PROJECT)"

    say ""
    say "${id} — agent \"${agent_name:-?}\""

    if [[ -z "$token" ]]; then
      warn "  !! no VROXY_SERVICE_TOKEN"
      warn "     fix: ./install.sh --dispatch (or --unattended with VROXY_TOKEN)"
      problems=$((problems + 1))
    fi

    if [[ -z "$install_id" ]]; then
      warn "  !! no VROXY_INSTALL_ID"
      warn "     fix: set one — python3 -c 'import uuid; print(uuid.uuid4().hex)'"
      problems=$((problems + 1))
    elif [[ -n "${install_ids[$install_id]:-}" ]]; then
      warn "  !! VROXY_INSTALL_ID ${install_id} is shared with ${install_ids[$install_id]} — two agents merge into one room"
      warn "     fix: give this instance a fresh id (the command above), not another instance's"
      problems=$((problems + 1))
    else
      install_ids[$install_id]="$id"
    fi

    if [[ -n "$agent_name" ]]; then
      if [[ -n "${agent_names[$agent_name]:-}" ]]; then
        warn "  !! agent name \"${agent_name}\" is also used by ${agent_names[$agent_name]} — names must be unique per workspace"
        problems=$((problems + 1))
      else
        agent_names[$agent_name]="$id"
      fi
    fi

    if [[ -n "$unit" && "$unit" != "vroxy-dispatch@${id}.service" ]]; then
      warn "  !! VROXY_DISPATCH_UNIT=${unit} should be vroxy-dispatch@${id}.service — self-update would restart the wrong unit"
      warn "     fix: sudo sed -i 's|^VROXY_DISPATCH_UNIT=.*|VROXY_DISPATCH_UNIT=vroxy-dispatch@${id}.service|' ${ENV_DIR}/${id}.env"
      problems=$((problems + 1))
    fi

    if [[ -z "$code_root" || -z "$project" ]]; then
      warn "  !! CODE_ROOT or PROJECT is missing"
      warn "     fix: ./install.sh --dispatch (or set both in ${ENV_DIR}/${id}.env)"
      problems=$((problems + 1))
    elif repo_resolves "$code_root" "$project"; then
      say "  ok  ${code_root}/${project}"
    elif is_git_repo "$code_root"; then
      say "  ok  single project ${code_root} (dispatch lifts it to $(dirname "$code_root") + $(basename "$code_root"))"
    else
      warn "  !! CODE_ROOT/PROJECT doesn't resolve: ${code_root}/${project} is not a directory"
      warn "     fix: CODE_ROOT is the PARENT folder and PROJECT the repo name under it, or point CODE_ROOT straight at the repo"
      problems=$((problems + 1))
    fi

    if systemctl is-active --quiet "vroxy-dispatch@${id}.service" 2>/dev/null; then
      say "  ok  unit active"
    else
      warn "  !! unit vroxy-dispatch@${id}.service is not active"
      warn "     fix: sudo systemctl enable --now vroxy-dispatch@${id}.service && journalctl -u vroxy-dispatch@${id} -n 50"
      problems=$((problems + 1))
    fi
  done

  say ""
  if [[ $problems -eq 0 ]]; then
    say "No problems found."
  else
    warn "$problems problem(s) found."
  fi
  return $(( problems > 0 ))
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
  ensure_python_toolchain
  if command -v pipx >/dev/null 2>&1; then
    pipx install --force "$HERE" >/dev/null || die "pipx install failed"
  elif python3 -m pip --version >/dev/null 2>&1 &&
       python3 -m pip install --user --quiet --upgrade "$HERE" 2>/dev/null; then
    :
  else
    # PEP 668: a distro-managed python refuses --user installs.  Own a
    # venv rather than arguing with it, and put the entry point on the
    # PATH by hand.  No root, nothing outside $HOME.
    if python3 -m pip --version >/dev/null 2>&1; then
      say "System python is externally managed — installing the CLI into its own venv."
    else
      say "System python has no pip — installing the CLI into its own venv."
    fi
    make_venv "$HERE/.venv-cli"
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
  local answer
  read_prompt answer $'\nAlso run a dispatch agent on this machine? It connects to one\nworkspace and installs a systemd unit. [y/N] ' || answer=""
  case "$answer" in
    [yY]*) add_workspace ;;
    *)     say "Skipped.  Run ./install.sh --dispatch later if you change your mind." ;;
  esac
}

migrate_legacy() {
  legacy_present || die "no legacy unit on this box — nothing to migrate."
  need python3

  local id="${1:-}"
  [[ -n "$id" ]] || { read_prompt id "Instance id for this workspace (e.g. vroxy): "; }
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
    read_prompt agent_name "Agent name: "
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

[[ "${BASH_SOURCE[0]:-$0}" == "$0" ]] || return 0

bootstrap_if_piped "$@"

case "${1:-}" in
  --unattended) unattended_workspace ;;
  --cli)        install_cli ;;
  --dispatch)   add_workspace ;;
  --migrate-legacy) migrate_legacy "${2:-}" ;;
  --update) update_all ;;
  --list)   list_instances ;;
  --doctor) doctor ;;
  --remove) remove_instance "${2:-}" ;;
  --help|-h)
    sed -n '2,20p' "$HERE/install.sh" | sed 's/^# \{0,1\}//'
    ;;
  "")       default_install ;;
  *)        die "unknown option: $1 (try --help)" ;;
esac
