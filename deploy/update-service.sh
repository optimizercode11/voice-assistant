#!/usr/bin/env bash
# Promote a prepared voice app while retaining the independent LLM service.
set -euo pipefail
cd "$(dirname "$0")/.."
read -r unit deploy_root < <(python3 -c 'import sys; sys.path.insert(0,"deploy"); import site_config as s; print(s.UNIT,s.DEPLOY_ROOT)')
[ "$PWD" = "$deploy_root" ] || { echo "Expected deployment root $deploy_root" >&2; exit 2; }
exec 9>/tmp/voice-stack-deploy.lock
flock -n 9 || exit 3
mkdir -p rollback
[ ! -e rollback/voice-unit.service ] || { echo 'Rollback snapshot already exists'; exit 3; }
conf="$HOME/.config/systemd/user/$unit"
cp "$conf" rollback/voice-unit.service
systemctl --user show q38-server -p MainPID > rollback/q38-pid.txt
systemctl --user show q36-server -p ActiveState -p UnitFileState > rollback/q36-state.txt
previous_root=$(systemctl --user show "$unit" -p WorkingDirectory --value)
if [ -f "$previous_root/var/approvals.json" ]; then
 mkdir -p var
 cp "$previous_root/var/approvals.json" var/approvals.json
fi
restore(){
 rc=$?; trap - EXIT
 if [ "$rc" -ne 0 ]; then
  systemctl --user stop "$unit" || true
  cp rollback/voice-unit.service "$conf"
  systemctl --user daemon-reload
  systemctl --user start "$unit"
  echo 'Restored previous voice deployment after failed update' >&2
 fi
 exit "$rc"
}
trap restore EXIT
systemctl --user stop "$unit"
install -m 644 "deploy/$unit" "$conf"
systemctl --user daemon-reload
python3 tools/voicectl.py --config config/host.toml index build
systemctl --user start "$unit"
systemctl --user is-active "$unit"
systemctl --user is-enabled "$unit"
systemctl --user show q38-server -p MainPID > run/q38-pid-after.txt
systemctl --user show q36-server -p ActiveState -p UnitFileState > run/q36-state-after.txt
cmp rollback/q38-pid.txt run/q38-pid-after.txt
cmp rollback/q36-state.txt run/q36-state-after.txt
trap - EXIT
