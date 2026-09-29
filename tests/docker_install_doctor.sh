#!/usr/bin/env bash
#
# Full install.sh --cli against a bare Ubuntu image — the path a
# fresh clone hits when python3 / pip / python3-venv are missing.
#
#   ./tests/docker_install_doctor.sh
#   ./tests/docker_install_doctor.sh ubuntu:24.04
#
# Needs docker. Pulls the image if missing. Each case copies the
# checkout WITHOUT host .venv debris so a false pass can't sneak in.

set -euo pipefail

IMAGE="${1:-ubuntu:24.04}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { say "  OK  $1"; PASS=$((PASS + 1)); }
bad()  { say "  FAIL $1"; say "       $2"; FAIL=$((FAIL + 1)); }

need_docker() {
  command -v docker >/dev/null 2>&1 || {
    say "docker is not installed — skipping install doctor"
    exit 0
  }
}

copy_src_cmd='
mkdir -p /work
tar -C /src --exclude=.venv --exclude=.venv-cli --exclude="*.egg-info" \
    --exclude=__pycache__ --exclude=.git --exclude=log --exclude=build -cf - . \
  | tar -C /work -xf -
cd /work
'

run_case() {
  local name="$1" prep="$2"
  say "==> $name"
  local out rc
  set +e
  out="$(docker run --rm \
    -v "$ROOT:/src:ro" \
    -e DEBIAN_FRONTEND=noninteractive \
    "$IMAGE" \
    bash -c "
      set -e
      $prep
      $copy_src_cmd
      ./install.sh --cli
      export PATH=\"\$HOME/.local/bin:\$PATH\"
      command -v vroxy >/dev/null
      vroxy --help >/dev/null
      vroxy --version
    " 2>&1)"
  rc=$?
  set -e
  if [[ $rc -eq 0 ]] && grep -q 'Installed the vroxy CLI' <<<"$out"; then
    ok "$name"
  else
    bad "$name" "exit $rc — last lines:"
    printf '%s\n' "$out" | tail -20 | sed 's/^/       /'
  fi
}

need_docker
say "Pulling $IMAGE…"
docker pull -q "$IMAGE" >/dev/null

run_case "bare image (no python)" "
  true
"

run_case "python3 only (no venv/pip)" "
  apt-get update -qq
  apt-get install -y -qq python3 ca-certificates >/dev/null
"

run_case "python3 + venv already present" "
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip ca-certificates >/dev/null
  ver=\$(python3 -c 'import sys; print(\"%d.%d\" % sys.version_info[:2])')
  apt-get install -y -qq \"python\${ver}-venv\" >/dev/null
"

say ""
say "install doctor: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
