#!/usr/bin/env bash
# Run from this campaign's fresh deployment tree through the CPU guard.
set -euo pipefail
cd "$(dirname "$0")/../../.."

unit=voice-stack-gpu4.service
state=$(systemctl --user show "$unit" -p ActiveState --value)
main_pid=$(systemctl --user show "$unit" -p MainPID --value)
group=$(systemctl --user show "$unit" -p ControlGroup --value)

# The previous boot passed speech acceptance but failed its GPU guard when
# unrelated services initialized concurrently. Its supervisor survived without
# a systemd MainPID. Stop only this unit's remaining processes before promotion.
if [ "$state" = failed ] && [ "$main_pid" = 0 ] && [ -n "$group" ]; then
    case "$group" in
        */voice-stack-gpu4.service) ;;
        *) echo "Unexpected voice control group: $group" >&2; exit 2 ;;
    esac
    members="/sys/fs/cgroup$group/cgroup.procs"
    if [ -s "$members" ] || { [ -f "$members" ] && read -r leftover < "$members"; }; then
        echo 'Stopping orphaned processes owned by the failed voice unit.'
        systemctl --user kill --kill-whom=all --signal=SIGTERM "$unit"
        for attempt in $(seq 1 90); do
            if [ ! -f "$members" ] || ! read -r leftover < "$members"; then break; fi
            sleep 1
        done
        if [ -f "$members" ] && read -r leftover < "$members"; then
            echo 'Voice unit still has processes after graceful shutdown; refusing promotion.' >&2
            exit 2
        fi
    fi
fi
systemctl --user reset-failed "$unit"
bash deploy/update-service.sh
