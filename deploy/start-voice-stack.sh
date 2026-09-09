#!/usr/bin/env bash
# What systemd runs.  The stack's GPU work happens inside THIS guarded run, so
# its containment is certified by the manifest this start records -- not by
# whatever run installed these files.  Campaign, device, CPU set and priority
# all come from deploy/site_config.py, so the unit and the guard cannot drift
# apart into two different stories about where this stack lives.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/source.env
export GUARD_GIT_HEAD GUARD_SOURCE_FP GUARD_INPUT_SCOPE=recorded_remote

read -r campaign gpu cpus nice < <(python3 -c '
import sys; sys.path.insert(0, "deploy")
import site_config as site
print(site.CAMPAIGN, site.GPU, site.CPUS, site.NICE)')

args=(--campaign "$campaign" --label gpu-acceptance --gpu "$gpu" --cpus "$cpus" --nice "$nice")
while IFS= read -r file; do args+=(--input "$file"); done < <(python3 deploy/voice_stack.py inputs)
exec ./.guarded-run "${args[@]}" -- python3 -u deploy/voice_stack.py start
