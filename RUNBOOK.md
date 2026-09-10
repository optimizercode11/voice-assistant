# Runbook

Everything here is a command that has been run, not a sketch.  The stack is one
systemd **user** service; user lingering is enabled on the host, so it starts at
boot without an interactive login.

## Operate

```bash
ssh vllm 'systemctl --user status voice-stack-gpu2'
ssh vllm 'journalctl --user -u voice-stack-gpu2 -n 100 --no-pager'
tail -f ~/qwen36/voice-stack/voice-restore-20260908/run/{supervisor,qwen,tts,stt,bridge}.log
```

`restart` and `stop` are GPU-affecting: do them inside a task that is
authorized for the device named in `deploy/site_config.py` (`GPU`), not as a
casual SSH command.

## Capabilities: check before you start

Tools, RAG and MCP are inert until a config names them, and the live site keeps
them inert: `site_config.py:TOOLS_CONFIG` is empty, so the supervisor does not
pass `--tools-config` and the bridge offers no tools at all.  That is deliberate
— a deployment that quietly started offering tools is no longer the thing
`PROVENANCE.md` certified.

To see what a config would allow, and what is already broken, without starting
anything:

```bash
python3 tools/voicectl.py --config config/assistant.toml doctor            # offline
python3 tools/voicectl.py --config config/assistant.toml doctor --probe    # starts the MCP servers
python3 tools/voicectl.py --config config/assistant.toml index build       # rebuild the RAG index
python3 tools/voicectl.py --config config/assistant.toml tools list        # exact specs the model sees
```

`doctor --probe` exits non-zero when a *configured* MCP server will not come up.
A dead server degrades the assistant — it comes up with its remaining tools and
records a note — so without the probe that degradation is invisible.

Enabling capabilities at a site is one line in `deploy/site_config.py`
(`TOOLS_CONFIG = "config/assistant.toml"`), a rebuilt index, and a restart of
the bridge inside a task authorized for the GPU it names.  The bridge refuses to
start on a config it cannot fully understand, so a typo in the capability file
costs a startup, not a half-loaded tool set.

## What "started" means

`systemctl` is not the authority.  The unit is `Type=forking` and its
`ExecStart` goes through `.guarded-run`, so:

1. the wrapper records a guard manifest for the start (`--label gpu-acceptance`);
2. `voice_stack.py start` verifies every pinned binary hash, the qasr
   `RELEASE.json`, and the 2000+ file language-runtime inventory **before**
   launching anything;
3. it then runs a real round trip — TTS a sentence, STT the audio, chat with
   the transcript, synthesize the reply, plus a Hindi reply and malformed
   request controls — and only then writes `run/ready` and hands systemd the
   supervisor PID.

A stack that is up but never passed its own round trip does not exist.  That is
why `run/acceptance.json` is read by the deploy gate instead of a log line.

## Install on a host

```bash
git clone … voice-assistant && cd voice-assistant
rsync -a --delete --exclude .git --exclude evidence ./ vllm:~/qwen36/voice-stack/voice-restore-20260908/
ssh vllm 'cd ~/qwen36/voice-stack/voice-restore-20260908 && \
  GUARD_GIT_HEAD=$(git -C /mnt/inference-engine rev-parse HEAD) bash deploy/install-service.sh'
```

`deploy/source.env` must carry the `GUARD_GIT_HEAD` / `GUARD_SOURCE_FP` of the
tree that was installed (`scripts/worktree-fingerprint .`); copy
`deploy/source.env.example` and fill it in.  `install-service.sh` refuses to run
from a checkout that is not `DEPLOY_ROOT`, refuses to overwrite an existing
`rollback/`, and restores the unit it replaces if enabling fails.

## Deploy a code change to the live stack

`deploy/campaigns/qasr-20260909/deploy.py` is the worked example: it binds the
release it is about to install, backs up the live files, installs, restarts,
waits for a *fresh* `run/acceptance.json`, and **rolls back on any failure
including a crash**.  Two rules in it are worth keeping in any successor:

* it is a **CPU** guarded run — the stack's GPU containment is certified by the
  stack's own guarded start, and restarting it from inside a GPU run makes the
  stack's PIDs look like foreign processes appearing on the card mid-run;
* it refuses rather than re-baselines: if the files it is about to install do
  not match `expected.json`, or a rollback copy no longer describes the system,
  it stops.

## Rollback

`stop_stack()` SIGTERMs the supervisor and waits; the supervisor terminates its
children in reverse order and waits.  No SIGKILL as a first action — escalating
into a 30 GiB model's teardown is how a device gets wedged.  If the stack does
not let go, the deploy refuses to start a second one.

## Gates

Evidence is recorded through the vendored harness in `scripts/` and closed with:

```bash
scripts/guardrail-check . --profile gpu-bugfix --campaign <campaign-id>
```

Profiles are `audit`, `bugfix`, `gpu-bugfix`, `perf`.  A campaign ID is
one-shot: pick it, pass it to every command, and never mix evidence from two
campaigns.  `deploy/campaigns/qasr-20260909/run_gate.sh` is a complete worked
gate in the order the checker requires.

## When the answer comes back silent

| Symptom | First place to look |
|---|---|
| Nothing happens on Start | Is the page HTTPS? `getUserMedia` is unavailable on the HTTP origin. |
| 503 "conversation model is busy" | Another reply holds `chat_lock`; one generation at a time. |
| 503 "speech-to-text engine is unavailable" | `run/stt.log`; `curl 127.0.0.1:8095/health`. |
| 400 "Cannot decode this audio" | ffmpeg is missing or the upload is not a media container. |
| 502 "reply was cut short" | The model hit `max_tokens 384`; ask something shorter. |
| Interrupt seems slow | The upstream is non-streaming: it may finish the bounded generation before serving the next turn. |
| Stack restarts in a loop | `StartLimitBurst=3` per 900 s; read `run/supervisor.log` for which child died. |
