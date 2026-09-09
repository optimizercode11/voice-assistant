#!/usr/bin/env bash
# Install and enable the user unit, with a rollback that restores whatever unit
# this one replaces.  Run on the host, from the live checkout:
#   bash deploy/install-service.sh
# Enabling a service is not a GPU command; starting it is what enters the guard
# (start-voice-stack.sh), which is why this script needs no device authorization.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
read -r unit deploy_root replaces < <(python3 -c '
import sys; sys.path.insert(0, "deploy")
import site_config as site
print(site.UNIT, site.DEPLOY_ROOT, "q38-server.service")')

if [ "$REPO" != "$deploy_root" ]; then
  echo "install-service: this checkout is $REPO but site_config says the live" >&2
  echo "deployment is $deploy_root; install from the live checkout, or point" >&2
  echo "VOICE_DEPLOY_ROOT at the right place deliberately." >&2
  exit 2
fi

mkdir -p rollback
[ ! -f rollback/legacy-unit.txt ] || { echo "rollback/legacy-unit.txt already exists; refusing to overwrite it" >&2; exit 2; }
systemctl --user cat "$replaces" > rollback/legacy-unit.txt
systemctl --user is-enabled "$replaces" > rollback/legacy-enabled.txt
systemctl --user show "$replaces" -p MainPID -p ExecStart > rollback/legacy-status.txt
install -m 644 deploy/voice-stack-gpu2.service "$HOME/.config/systemd/user/$unit"
systemctl --user daemon-reload

restore() {
    result=$?
    if [ "$result" -ne 0 ]; then
        systemctl --user stop "$unit" || true
        systemctl --user disable "$unit" || true
        systemctl --user enable --now "$replaces" || true
        echo "Voice deployment failed; restored $replaces" >&2
    fi
    exit "$result"
}
trap restore EXIT
systemctl --user disable --now "$replaces"
systemctl --user start "$unit"
systemctl --user enable "$unit"
systemctl --user is-active "$unit"
systemctl --user is-enabled "$unit"
