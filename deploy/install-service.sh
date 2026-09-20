#!/usr/bin/env bash
# Switch GPU 4 from Qwen3.6 to speech, after the user authorizes replacement.
# The external q38-server.service on GPU 2 is never changed here.
set -euo pipefail
cd "$(dirname "$0")/.."
read -r unit deploy_root < <(python3 -c '
import sys; sys.path.insert(0, "deploy")
import site_config as site
print(site.UNIT, site.DEPLOY_ROOT)')
[ "$PWD" = "$deploy_root" ] || { echo "Run from $deploy_root" >&2; exit 2; }
[ "${VOICE_REPLACE_Q36:-}" = 1 ] || { echo 'Set VOICE_REPLACE_Q36=1 only after replacement is authorized.' >&2; exit 2; }
[ ! -e rollback/service-states.json ] || { echo 'Rollback snapshot already exists; refusing to overwrite it.' >&2; exit 2; }
mkdir -p rollback
python3 - <<'PY'
import json, pathlib, subprocess
services = ['q36-server.service', 'voice-tools-bridge.service', 'voice-stack-gpu2.service', 'voice-stack-gpu4.service']
states = {}
for unit in services:
    states[unit] = {key: subprocess.run(['systemctl', '--user', command, unit], capture_output=True, text=True).stdout.strip()
                   for key, command in [('active', 'is-active'), ('enabled', 'is-enabled')]}
    source = pathlib.Path.home()/'.config/systemd/user'/unit
    if source.exists(): (pathlib.Path('rollback')/unit).write_bytes(source.read_bytes())
pathlib.Path('rollback/service-states.json').write_text(json.dumps(states, indent=2)+'\n')
PY
# Preserve the old app and its approval store in place. No user data is copied
# into a new deployment, whose directory grants start empty.
systemctl --user cat q38-server.service > rollback/q38-untouched.txt
systemctl --user show q38-server.service -p MainPID > rollback/q38-pid.txt
rollback() {
    rc=$?
    if [ "$rc" -ne 0 ]; then
        systemctl --user stop "$unit" || true
        systemctl --user disable "$unit" || true
        python3 deploy/restore-service.py
        echo 'Voice switchover failed; prior service states restored.' >&2
    fi
    exit "$rc"
}
trap rollback EXIT
install -m 644 "deploy/$unit" "$HOME/.config/systemd/user/$unit"
systemctl --user daemon-reload
systemctl --user disable --now q36-server.service
systemctl --user disable --now voice-tools-bridge.service
systemctl --user disable voice-stack-gpu2.service
CUDA_VISIBLE_DEVICES='' python3 tools/voicectl.py --config config/host.toml index build
systemctl --user start "$unit"
systemctl --user enable "$unit"
systemctl --user is-active "$unit"
systemctl --user is-enabled "$unit"
systemctl --user show q38-server.service -p MainPID > run/q38-pid-after.txt
cmp rollback/q38-pid.txt run/q38-pid-after.txt
