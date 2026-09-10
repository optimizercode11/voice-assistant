#!/usr/bin/env python3
"""One place where this repo stops being portable and becomes THIS host.

Everything else in the repo is relative to the checkout.  This file is the
deployment profile: absolute paths to model checkpoints, the accepted native
binaries and their SHA-256 pins, ports, the TLS certificate, and the GPU.  It
was lifted out of ``voice_stack.py`` (and of the campaign scripts that each
repeated a copy of it) so that a new site is one file, not a grep.

The values are the ones the live GPU-2 stack runs today; ``PROVENANCE.md``
names the campaign that certified them.

Overrides (environment, for a different site or a rehearsal):

    VOICE_HOST_ROOT    directory holding the per-campaign runtime trees
    VOICE_DEPLOY_ROOT  where the live checkout lives on the host
    VOICE_UNIT         systemd user unit that owns the stack
    VOICE_CAMPAIGN     campaign id the service wrapper records under
    VOICE_GPU          GPU index the stack is authorized to use
"""
from __future__ import annotations

import os
from pathlib import Path


def _path(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable, default))


def _text(variable: str, default: str) -> str:
    return os.environ.get(variable, default)


# --------------------------------------------------------------------------
# Host layout: per-campaign runtime trees left behind by earlier campaigns.
# These are *inputs*, not build outputs: the stack reuses the accepted
# binaries instead of rebuilding them, so a missing directory here means a
# rebuild campaign, not a path edit.  Keep them; do not "clean" them.
# --------------------------------------------------------------------------
HOST_ROOT = _path("VOICE_HOST_ROOT", "/home/ambudsharma/qwen36")

# Multilingual G2P runtime (pinned python tree + its inventory manifest).
LANGUAGES = HOST_ROOT / "kokoro-codex-kokoro-languages-20260908/kokoro-languages-20260908"
# Pinned ffmpeg (imageio_ffmpeg build) and the legacy native vvasr binary.
PRIOR = HOST_ROOT / "kokoro-codex-stt-tts-ui-20260908/stt-tts-ui-20260908"
# The certificate the user's browser already trusts, from the recording campaign.
TLS_DIR = HOST_ROOT / "kokoro-codex-stt-record-20260908/stt-record-20260908/runtime/tls"
CERT = TLS_DIR / "cert.pem"
KEY = TLS_DIR / "key.pem"

FFMPEG = PRIOR / "runtime/ffmpeg/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"

# --------------------------------------------------------------------------
# Model checkpoints.  These live on the HOST's /mnt, which is not the dev
# VM's /mnt: "No such file" locally says nothing about the host.
# --------------------------------------------------------------------------
LLM_MODEL = Path("/mnt/395/hub/models--unsloth--Qwen3.8-27B-NVFP4/"
                 "snapshots/38bd651c3a121aa4c6dbe5e5acc1e0b82dc1f5c3")
TTS_MODEL_DIR = Path("/mnt/kokoro")
TTS_CONFIG = TTS_MODEL_DIR / "config.json"
TTS_WEIGHTS = TTS_MODEL_DIR / "campaigns/kokoro-cb-20260907/oracle/weights_eff.safetensors"
TTS_REF_S = TTS_MODEL_DIR / "campaigns/kokoro-cb-20260907/oracle/row_00/ref_s.npy"
TTS_VOICES = TTS_MODEL_DIR / "voices_npy"
ASR_MODEL = Path("/mnt/vibevoice-asr-4262d23d8a539a6530cf64fbd0b1751ef9a30853")
ASR_TOKENIZER = Path("/mnt/vvasr-native-oracle/tokenizer.bin")
QASR_MODEL = Path("/mnt/qwen3-asr-0.6b-5eb144179a02acc5e5ba31e748d22b0cf3e303b0")

# --------------------------------------------------------------------------
# Accepted binaries and releases.  Qwen/TTS/vvasr are pinned by hash: a stale
# binary must fail at startup, not at 3am.  The resident ASR server carries
# its own RELEASE.json, so it is verified against itself instead of a hash
# hand-copied in here.
# --------------------------------------------------------------------------
LLM_BINARY = HOST_ROOT / ("kokoro-codex-voice-chat-20260908/voice-chat-20260908/"
                          "runtime/qwen/q38-27-nvfp4/q38_27_server")
TTS_BINARY = HOST_ROOT / "kokoro-codex-kokoro-cb-20260907/kokoro-cb-20260907/kserver"
ASR_BINARY = PRIOR / "runtime/asr/vibevoice-asr/vvasr"          # legacy per-request path
QASR_RELEASE = HOST_ROOT / "qasr-serve-release-20260909/qasr-serve-deploy-20260909"
QASR_WORKER = QASR_RELEASE / "qasr_worker"
QASR_SERVE = QASR_RELEASE / "tools/qasr_serve.py"
QASR_FRONTEND = QASR_RELEASE / "reference/upstream"
# Pinned transformers 4.57.6 tree: the frontend authority that wrote the oracle.
QASR_ENV = HOST_ROOT / "oracle-envs/qwen3-asr-runtime-4.57.6/python"
# Interpreters: G2P owns the multilingual misaki/espeak environment, qasr_serve
# runs on the host venv and reaches the pinned transformers 4.57.6 tree through
# PYTHONPATH.  Neither is the system python.
G2P_PYTHON = TTS_MODEL_DIR / "oracle-env/bin/python"
QASR_PYTHON = Path("/mnt/.venv/bin/python")

PINS = {
    LLM_BINARY: "60bdab2564539d5067796c2f3df171b2da1168088dcb7f335243c55e3d319050",
    TTS_BINARY: "a4da4411f3f2ab026f2255f1fa2d13404ca2d3d17eef07d1f8a81d48c7d4efae",
    ASR_BINARY: "0b7d715043d00448427669ba62c92d065d5193833d6ad2143b0a6c77f6bedf7b",
}

# --------------------------------------------------------------------------
# Listening ports and the address the user's browser reaches.
# --------------------------------------------------------------------------
LLM_PORT = 8080
TTS_PORT = 8090
HTTP_PORT = 8091
HTTPS_PORT = 8092
QASR_PORT = 8095
PORTS = (LLM_PORT, TTS_PORT, HTTP_PORT, HTTPS_PORT, QASR_PORT)
LAN_ORIGIN = "192.168.228.113"
CHAT_URL = f"https://{LAN_ORIGIN}:{HTTPS_PORT}/chat"
STUDIO_URL = f"https://{LAN_ORIGIN}:{HTTPS_PORT}/"

# The G2P sidecar and kserver meet on this unix socket.
G2P_SOCKET = _text("VOICE_G2P_SOCKET", "/tmp/kokoro-voice-restore-20260908.sock")

# --------------------------------------------------------------------------
# Device authorization.  GPU 2 is the voice stack's authorized device; the
# guard, the supervisor and the acceptance scripts all read it from here.
# --------------------------------------------------------------------------
GPU = _text("VOICE_GPU", "2")
GPU_UUID = "GPU-79d34900-581c-5b37-3dde-f53a8592181a"
CPUS = "24-27"
NICE = 10

# --------------------------------------------------------------------------
# The live deployment: where the checkout is installed on the host, the unit
# that owns it, and the campaign its guarded start records evidence under.
# --------------------------------------------------------------------------
DEPLOY_ROOT = _path("VOICE_DEPLOY_ROOT", str(HOST_ROOT / "voice-stack/voice-restore-20260908"))
UNIT = _text("VOICE_UNIT", "voice-stack-gpu2.service")
CAMPAIGN = _text("VOICE_CAMPAIGN", "voice-restore-20260908")
# Written by the deploy campaign; the supervisor's evidence lands beside it.
DEPLOY_RECORDS = DEPLOY_ROOT / "evidence/deploy-qasr-20260909"

# Sampled per-process peaks on this 32607 MiB card, for context only: Qwen
# 23372 MiB, ASR 5268 MiB, TTS 1288 MiB.  A conservative sum of samples, not
# a worst-case memory guarantee.
GPU_MEMORY_MIB = 32607

# --------------------------------------------------------------------------
# Capabilities: tools, RAG, MCP.  Empty means "this site offers none", which
# is what the live stack was certified with.  Naming a file here is the only
# way the supervisor passes --tools-config, because a deployment that quietly
# started offering tools is no longer the thing PROVENANCE.md certified.
# Set VOICE_TOOLS_CONFIG=config/assistant.toml to turn them on for a site.
# --------------------------------------------------------------------------
TOOLS_CONFIG = _text("VOICE_TOOLS_CONFIG", "")
