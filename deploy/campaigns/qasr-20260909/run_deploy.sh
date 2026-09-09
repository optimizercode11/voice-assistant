#!/usr/bin/env bash
# Install this checkout's bridge + supervisor onto the live stack and restart it.
#
#   bash deploy/campaigns/qasr-20260909/run_deploy.sh
#
# A CPU guarded run on purpose (deploy.py explains why): the stack's GPU work
# happens inside its OWN guarded start, whose manifest acceptance.py then reads.
# Everything that gets installed or installed-over is bound with --input, so the
# manifest names the exact bytes that went in.
#
# NOTE: this campaign is CLOSED and green in the inference-engine repo.  Read
# README.md here before re-running: expected.json still records the hashes that
# campaign installed, and deploy.py refuses -- rather than re-baselines -- when
# the files it is about to install do not match it.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
CAMPAIGN="${VOICE_CAMPAIGN:-qasr-deploy-20260909-b}"
read -r cpus nice release < <(python3 -c '
import sys; sys.path.insert(0, "deploy")
import site_config as site
print(site.CPUS, site.NICE, site.QASR_RELEASE)')

args=(. --campaign "$CAMPAIGN" --label positive --cpu --cpus "$cpus" --nice "$nice")
for dir in tools web deploy scripts; do args+=(--file "$dir"); done
for file in "$release/RELEASE.json" "$release/qasr_worker" "$release/tools/qasr_serve.py" \
            deploy/campaigns/qasr-20260909/expected.json; do
  args+=(--input "$file")
done
exec scripts/guarded-hostrun "${args[@]}" -- python3 deploy/campaigns/qasr-20260909/deploy.py
