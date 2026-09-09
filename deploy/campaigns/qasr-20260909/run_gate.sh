#!/usr/bin/env bash
# Re-record the whole gpu-bugfix profile for the ASR-residency deploy under ONE
# campaign, in the order guardrail-check requires, and finish with the harness's
# own verdict rather than this script's opinion of it.
#
#   bash deploy/campaigns/qasr-20260909/run_gate.sh
#
# Deploys and measurement windows are scripts, not sessions.  reproduce and
# regression are CPU guarded runs; positive is one too on purpose (deploy.py);
# negative and gpu-acceptance need the authorized device named in site_config.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
CAMPAIGN="${VOICE_CAMPAIGN:-qasr-deploy-20260909-b}"
read -r gpu cpus nice < <(python3 -c '
import sys; sys.path.insert(0, "deploy")
import site_config as site
print(site.GPU, site.CPUS, site.NICE)')
# tools/ and web/ go over as directories: speech_ui.py imports voice_chat and
# defaults --page to web/index.html, and a hand-picked file list that omits a
# real dependency only works when a previous run left the file behind.
SYNC=(--file tools --file web --file deploy --file scripts)
CAMPAIGN_DIR=deploy/campaigns/qasr-20260909

echo "=== campaign: $CAMPAIGN ==="

echo "--- 1/5 reproduce (expect rc=2: two requests, two processes) ---"
scripts/guarded-hostrun . --campaign "$CAMPAIGN" --label reproduce \
  --cpu --cpus "$cpus" --nice "$nice" --expect-exit 2 \
  "${SYNC[@]}" --input "$CAMPAIGN_DIR/reproduce.py" \
  -- python3 "$CAMPAIGN_DIR/reproduce.py"

echo "--- 2/5 positive (the deploy itself) ---"
VOICE_CAMPAIGN="$CAMPAIGN" bash "$CAMPAIGN_DIR/run_deploy.sh"

echo "--- 3/5 negative (a dead engine is never a silent 200) ---"
scripts/guarded-hostrun . --campaign "$CAMPAIGN" --label negative \
  --gpu "$gpu" --cpus "$cpus" --nice "$nice" \
  "${SYNC[@]}" --input "$CAMPAIGN_DIR/negative.py" \
  -- python3 "$CAMPAIGN_DIR/negative.py"

echo "--- 4/5 regression (the bridge's own suites) ---"
scripts/guarded-hostrun . --campaign "$CAMPAIGN" --label regression \
  --cpu --cpus "$cpus" --nice "$nice" \
  "${SYNC[@]}" --file tests \
  -- python3 -c "import subprocess,sys; sys.exit(sum(bool(subprocess.run([sys.executable,'-u',t]).returncode) for t in ('tests/speech_ui_test.py','tests/voice_chat_test.py')))"

echo "--- 5/5 gpu-acceptance (residency, round trip, the 30 s bound) ---"
scripts/guarded-hostrun . --campaign "$CAMPAIGN" --label gpu-acceptance \
  --gpu "$gpu" --cpus "$cpus" --nice "$nice" \
  "${SYNC[@]}" --input "$CAMPAIGN_DIR/acceptance.py" \
  -- python3 "$CAMPAIGN_DIR/acceptance.py"

echo "=== verdict ==="
scripts/guardrail-check . --profile gpu-bugfix --campaign "$CAMPAIGN"
