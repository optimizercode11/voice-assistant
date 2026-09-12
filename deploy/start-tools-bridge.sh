#!/usr/bin/env bash
# What systemd runs for the tools bridge.
#
# Same guard, same CPU set and priority as the certified stack -- but --cpu.
# This process is http.server plus ffmpeg and initialises no device; the GPU
# work it depends on is already resident on device 2, owned by
# voice-stack-gpu2.  Claiming --gpu here would attribute someone else's
# 27 GiB of device PIDs to this run's process group, which is a worse manifest,
# not a stronger one.
#
# That this can run at all is the point of scoping speech_ui.py's
# CUDA_VISIBLE_DEVICES requirement to the native vvasr path: with --asr-url the
# bridge never touches a device, and demanding one was a demand to claim a GPU
# rather than to use it.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f deploy/source.env ] && source deploy/source.env
export GUARD_GIT_HEAD="${GUARD_GIT_HEAD:-unrecorded}" \
       GUARD_SOURCE_FP="${GUARD_SOURCE_FP:-unrecorded}" \
       GUARD_INPUT_SCOPE=recorded_remote

read -r campaign cpus nice http https cert key ffmpeg < <(python3 -c '
import sys; sys.path.insert(0, "deploy")
import site_config as site
print(site.TOOLS_CAMPAIGN, site.CPUS, site.NICE, site.TOOLS_HTTP_PORT, site.TOOLS_HTTPS_PORT,
      site.CERT, site.KEY, site.FFMPEG)')

mkdir -p var
exec ./.guarded-run --campaign "$campaign" --label bridge-start --cpu --cpus "$cpus" --nice "$nice" -- \
  python3 -u tools/speech_ui.py \
    --host 0.0.0.0 --port "$http" --https-port "$https" \
    --tls-cert "$cert" --tls-key "$key" --ffmpeg "$ffmpeg" \
    --asr-url "http://127.0.0.1:8095" \
    --llm-url "http://127.0.0.1:8038" \
    --tools-config config/host.toml
