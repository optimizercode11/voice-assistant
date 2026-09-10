# Runbook

This is the app deployment procedure for the existing GPU-2 host. Commands
below were either run read-only or assembled from the cited scripts/manuals.
**No host-changing deployment, restart or rollback was executed in this
documentation review (2026-09-10).** CPU checks do not certify acoustic barge-in.

## Which service to touch

| Service | Tree under `vllm:~/qwen36/voice-stack/` | Browser URL | Ownership |
|---|---|---|---|
| `voice-tools-bridge.service` | `voice-tools-20260910` | https://192.168.228.113:8094/chat | CPU bridge, tools/RAG/MCP; target of this procedure |
| `voice-stack-gpu2.service` | `voice-restore-20260908` | https://192.168.228.113:8092/chat | Resident models on GPU 2 plus the original bridge; leave running |

Read `deploy/voice-tools-bridge.service:7` and `:14`: the tools unit has its
own working directory and `Wants=voice-stack-gpu2.service`. We use
`--job-mode=ignore-dependencies` for its lifecycle commands so an inactive
dependency cannot be started as a side effect. Both units must already be
healthy before deploying the app.

**Do not run `bash deploy/install-service.sh` for this update.** Its actual
body installs the GPU unit (`deploy/install-service.sh:27`), disables/stops
`q38-server.service` (`:41`), and starts `voice-stack-gpu2.service` (`:42`).
There is no `--tools-only` flag. The existing ASR campaign also stops and starts
the GPU stack (`deploy/campaigns/qasr-20260909/deploy.py:301`). Neither is a
shortcut for this procedure.

## What the user will feel — a 30-second check

Open `:8094/chat` over trusted HTTPS, choose the intended microphone/output,
leave **Interrupt replies by speaking** checked, then **Start conversation**.
Ask “Describe a forest in five sentences.” Start the timer when the reply
becomes audible; allow model latency between checks if it exceeds this budget.

1. **0–5 s:** While the reply is audible, stay quiet.
   It should keep playing without answering its own echo.
   The orb's glow should follow the spoken syllables.
2. **5–12 s:** After at least a second of playback, speak “Wait, change that.”
   In supported operation the audio cuts off mid-reply and the state changes
   from **Speaking** to **Listening**. This is an abrupt stop, not a fade.
   The glow then follows your microphone. The gate requires 220 ms of sustained
   energy (`web/chat.js`, `BARGE.holdMs`); that is not a latency guarantee.
3. **12–22 s:** Once **Listening** appears, say “Tell me one fact about rivers.”
   The opening interruption was only detected, not buffered into the new
   recording. Keep talking or repeat the request. Pause and check the transcript
   and replacement answer; the stopped audio must not resume.
4. **22–30 s:** Uncheck the feature, end and restart the conversation, ask again,
   and use **Interrupt reply** or Space outside a text field/control. Confirm
   that manual interruption still returns to listening.

**Reload the tab mid-conversation** before you finish.  The transcript comes
back, nothing is re-spoken, and the next question is still answered *in
context* — the header beside "Your conversation" says how many turns are saved
in this browser and which window Qwen is holding.  A reload that restores the
bubbles but sends an empty context is a failure, not a cosmetic pass: the bridge
is stateless, so the page is the model’s memory.

If `barge-note` says **“Unavailable: the reply is not routed through
WebAudio.”** or **“Unavailable on this audio device: no echo cancellation.”**,
record that refusal and exercise the button fallback; do not call voice
interruption a pass. Check the browser/device/output, checkbox, audible cut,
orb/state transition, transcript and result in the release record. Headless
waveform tests cannot replace this speaker-to-microphone check. This review
did not perform it.

## Barge-in modes and limits

The browser supplies echo cancellation (AEC3 on libwebrtc paths); the page
adds the playback-reference RMS gate. `web/chat.js` requests
`autoGainControl: !bargeWanted()` so automatic gain does not lift quiet echo
above its fixed threshold. Some Bluetooth and raw Linux paths report no
cancellation; the actual rule is `bargeAvailable()` requiring
`echoCancellation === true`. Missing settings also refuse. Uncheck the
feature and restart the conversation to change capture constraints.

Limits to expect:

- `web/chat.js::startBargeWatch()` checks `phase === 'speaking'`. Speech during
  `thinking` or `synthesizing` does not interrupt; use the button/Space then.
- `web/chat.js::listen()` charges `BARGE.settleMs = 350` only while the reply is
  genuinely still settling: the window is computed from
  `performance.now() - playbackEndedAt`, so a clip long after playback pays
  nothing. It used to ask whether `playbackEndedAt` was non-zero — a timestamp
  read as a flag — which charged 350 ms of dead endpointing to every clip after
  the first reply. `tests/echo_path_test.mjs` now forbids that shape.
- `web/chat.js::runTurn()` holds only while `fragmentHolds < 3`. Four breaths
  ending unfinished clips still dispatch an accumulated partial sentence.
- `web/chat.js::nearEndSpeech()` uses one scalar `BARGE.echoGain = 0.5`, with
  `BARGE.floor = 0.020`. This is not an adaptive echo filter.
- The playback reference is built with `ctx.createMediaElementSource`, a method
  of `AudioContext`. The first version asked the media element for it, which is
  a capability check no browser can pass: the graph was never built, so the orb
  never glowed while the assistant spoke and barge-in refused on every device.
  `voice_barge_browser.mjs` now asserts the reference is real by requiring the
  glow to move during playback. See
  [Architecture](ARCHITECTURE.md#limits-and-current-defects) for the API source.

## Read-only preflight evidence — 2026-09-10

Run this from the source checkout:

```bash
CUDA_VISIBLE_DEVICES="" python3 deploy/check_site.py --compare-live
```

Actual output here (exit 1):

```text
== site profile: /home/ambudsharma/qwen36/voice-stack/voice-restore-20260908 unit=voice-stack-gpu2.service gpu=2 ==
  ok   ports are unique: (8080, 8090, 8091, 8092, 8095)
  ok   HTTP and HTTPS listeners use different ports
  ok   campaign id is guard-safe: voice-restore-20260908
  ok   3 binaries are pinned by full sha256
  ok   certificate and key are a pair in one directory
  ok   unit WorkingDirectory=['%h/qwen36/voice-stack/voice-restore-20260908'] points inside %h/qwen36/voice-stack/voice-restore-20260908
  ok   unit PIDFile=['%h/qwen36/voice-stack/voice-restore-20260908/run/stack.pid'] points inside %h/qwen36/voice-stack/voice-restore-20260908
  ok   unit ExecStart=['/bin/bash %h/qwen36/voice-stack/voice-restore-20260908/deploy/start-voice-stack.sh'] points inside %h/qwen36/voice-stack/voice-restore-20260908
  ok   unit ExecStart is the guarded start wrapper
  ok   start wrapper takes campaign/device/inputs from site_config, not literals
  ok   guard binds 48 inputs (checkpoints, binaries, source)
  ok   inputs include checkpoint/config assets
  ok   inputs include executables
  ok   inputs include files from this checkout
  ok   inputs bind tools/speech_ui.py
  ok   inputs bind web/chat.html
  ok   inputs bind web/chat.js
  ok   inputs bind deploy/site_config.py
  ok   inputs bind deploy/voice_stack.py
Traceback (most recent call last):
  File "/tmp/voice-lanes/wt/ship-docs/deploy/check_site.py", line 119, in <module>
    raise SystemExit(main())
                     ^^^^^^
  File "/tmp/voice-lanes/wt/ship-docs/deploy/check_site.py", line 94, in main
    live = subprocess.run([sys.executable, "-u",
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/subprocess.py", line 548, in run
    with Popen(*popenargs, **kwargs) as process:
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/subprocess.py", line 1026, in __init__
    self._execute_child(args, executable, preexec_fn, close_fds,
  File "/usr/lib/python3.12/subprocess.py", line 1955, in _execute_child
    raise child_exception_type(errno_num, err_msg, err_filename)
FileNotFoundError: [Errno 2] No such file or directory: PosixPath('/home/ambudsharma/qwen36/voice-stack/voice-restore-20260908')
```

The checker does not SSH. It opens the host's deployment path on this machine
(`deploy/check_site.py:93`), so this is not a credential failure.

SSH was available. This command runs the *already installed tools tree's*
checker against the original GPU-stack tree, suppressing Python bytecode writes:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'cd ~/qwen36/voice-stack/voice-tools-20260910 && PYTHONDONTWRITEBYTECODE=1 CUDA_VISIBLE_DEVICES="" python3 deploy/check_site.py --compare-live'
```

Actual output (exit 1):

```text
== site profile: /home/ambudsharma/qwen36/voice-stack/voice-restore-20260908 unit=voice-stack-gpu2.service gpu=2 ==
  ok   ports are unique: (8080, 8090, 8091, 8092, 8095)
  ok   HTTP and HTTPS listeners use different ports
  ok   campaign id is guard-safe: voice-restore-20260908
  ok   3 binaries are pinned by full sha256
  ok   certificate and key are a pair in one directory
  ok   unit WorkingDirectory=['%h/qwen36/voice-stack/voice-restore-20260908'] points inside %h/qwen36/voice-stack/voice-restore-20260908
  ok   unit PIDFile=['%h/qwen36/voice-stack/voice-restore-20260908/run/stack.pid'] points inside %h/qwen36/voice-stack/voice-restore-20260908
  ok   unit ExecStart=['/bin/bash %h/qwen36/voice-stack/voice-restore-20260908/deploy/start-voice-stack.sh'] points inside %h/qwen36/voice-stack/voice-restore-20260908
  ok   unit ExecStart is the guarded start wrapper
  ok   start wrapper takes campaign/device/inputs from site_config, not literals
  ok   guard binds 105 inputs (checkpoints, binaries, source)
  ok   inputs include checkpoint/config assets
  ok   inputs include executables
  ok   inputs include files from this checkout
  ok   inputs bind tools/speech_ui.py
  ok   inputs bind web/chat.html
  ok   inputs bind web/chat.js
  ok   inputs bind deploy/site_config.py
  ok   inputs bind deploy/voice_stack.py
  FAIL live and repo bind the same 86 inputs (+5 -5 added=['/home/ambudsharma/qwen36/qasr-serve-release-20260909/qasr-serve-deploy-20260909/qasr_worker', '/home/ambudsharma/qwen36/qasr-serve-release-20260909/qasr-serve-deploy-20260909/RELEASE.json', '/home/ambudsharma/qwen36/qasr-serve-release-20260909/qasr-serve-deploy-20260909/tools/qasr_serve.py'] removed=['/home/ambudsharma/qwen36/qasr-fix-deploy/qasr-fix-deploy-20260910/qasr_worker', '/home/ambudsharma/qwen36/qasr-fix-deploy/qasr-fix-deploy-20260910/RELEASE.json', '/home/ambudsharma/qwen36/qasr-fix-deploy/qasr-fix-deploy-20260910/tools/qasr_serve.py'])
== FAIL: 19 checks, 1 failures ==
```

This exposes a stale ASR profile in the tools tree. The current source's
`deploy/site_config.py:79` already names `qasr-fix-deploy-20260910`.
`--compare-live` compares external asset paths and explicitly removes both
checkout-local prefixes (`deploy/check_site.py:102`); it does **not** detect
stale `web/` or `tools/` bytes, nor verify the active processes.

This checksum dry run is the app-staleness check (the `n` means no writes):

```bash
rsync -aRnic web tools deploy/site_config.py vllm:~/qwen36/voice-stack/voice-tools-20260910/
```

Actual output (exit 0; a dry-run exit 0 does not mean the files match):

```text
.d..t...... deploy/
<fcst...... deploy/site_config.py
.d..t...... tools/
<fcst...... tools/agent_config.py
<fcst...... tools/agent_tools.py
<f+++++++++ tools/approvals.py
.f..t...... tools/g2p_sidecar.py
.f..t...... tools/mcp_client.py
<fcst...... tools/mcp_files.py
.f..t...... tools/mcp_stack_status.py
.f..t...... tools/retrieval.py
<fcst...... tools/speech_ui.py
<fcst...... tools/turn_control.py
.f..t...... tools/voice_chat.py
.f..t...... tools/voicectl.py
.d..t...... web/
<fcst...... web/chat.html
<fcst...... web/chat.js
.f..t...... web/index.html
```

Read-only commands also run:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'systemctl --user status voice-tools-bridge.service voice-stack-gpu2.service --no-pager -l'
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'systemctl --user show voice-stack-gpu2.service -p MainPID -p ActiveEnterTimestamp'
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'git -C ~/qwen36/voice-stack/voice-tools-20260910 log -1 --format=fuller'
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'curl -fsS --max-time 10 --cacert ~/qwen36/kokoro-codex-stt-record-20260908/stt-record-20260908/runtime/tls/cert.pem https://127.0.0.1:8094/chat/health'
```

Results: tools service active since `2026-09-10 02:04:24 UTC`, main PID
`422530`; GPU stack active since `2026-09-10 03:29:25 UTC`, main PID
`629273`. Verified TLS health returned `available: true`, HTTPS port `8094`,
nine tools, both MCP peers ready, and retrieval `ready: true, stale: false`.
Health describes the bridge configuration; it is not a real STT/chat/TTS turn.
Git exited 128 with the exact error:

```text
fatal: not a git repository (or any of the parent directories): .git
```

The installed tree is an rsync copy. Its `deploy/source.env` recorded head
`54bd429def00485bbb63008ee063d28e89cab8cd` and source fingerprint
`8896941aa46c472c2f7cd5947333039b3062f2c7159331684f2922e3d2558ec0`.
Fresh source-vs-host SHA-256 reads also showed:

| File | Source SHA-256 | Installed SHA-256 |
|---|---|---|
| `web/chat.js` | `d272adf99e51ba45fa52636dceaf16de4fa167283ec5e2db1fafe6d2ac4b8dc8` | `3c15b136d179aeaba506e57f49cdc8bd585f572dc9135746b91f2015a4777308` |
| `tools/speech_ui.py` | `b457791e07bf1e11ed6b87658b06027adf18f6f4f0d5e01ab9873825f56a3a31` | `f47f041a245f71b859a1f651db5793452511b3987f6ab4c29ea1c638891ecb3f` |

These are dated observations, not expected hashes for a later merged tree.

## Ship the current app to the CPU bridge

Run from the final integrated checkout. The local checks must pass. The two
findings above (the `AudioContext` capability check and the settle window) are
resolved in this tree; any new one must be resolved or recorded as a remaining
defect before claiming barge-in works. Have no active conversation
during the copy/restart. Stop at a failed step; do not substitute a GPU restart.

### 1. Check the candidate and refresh the live baseline

```bash
CUDA_VISIBLE_DEVICES="" python3 -u deploy/check_site.py ; echo "check-site=$?"
CUDA_VISIBLE_DEVICES="" make check-site ; echo "make=$?"
```

Run the compare-live, rsync dry-run, status, PID and TLS health commands from
the preflight section again; retain their fresh output. The local compare-live
failure above is expected off-host, while the candidate's on-host comparison
in step 2 must pass. Record the GPU PID/start timestamp for the post-check.

Sources: `deploy/check_site.py:39` and `Makefile:113` (`check-site`). Both local
commands passed with **19 checks, 0 failures**, `check-site=0` and `make=0`.
Read-only status
and TLS commands were also run exactly as printed above.

### 2. Stage through the shipped guard, in a new campaign directory

`voice-barge-20260910-a` **has now been used** — it is the campaign that shipped
barge-in on 2026-09-10, recorded at the end of this section. Choose a fresh
campaign ID, and replace it consistently throughout these steps.
This is the first host-writing step. `guarded-hostrun` performs mkdir, rsync,
remote execution and evidence retrieval even for a check payload.

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'test ! -e ~/qwen36/voice-stage/voice-barge-20260910-a' || exit 1
stage_args=(. --campaign voice-barge-20260910-a --label stage
  --cpu --cpus 24-27 --nice 10
  --dest qwen36/voice-stage/voice-barge-20260910-a)
for path in web tools deploy scripts config README.md ARCHITECTURE.md RUNBOOK.md TOOLS.md PROVENANCE.md tests/README.md; do
  stage_args+=(--file "$path")
done
for path in web/* tools/*.py deploy/*.py deploy/*.sh deploy/*.service scripts/* config/*.toml README.md ARCHITECTURE.md RUNBOOK.md TOOLS.md PROVENANCE.md tests/README.md; do
  [ ! -f "$path" ] || stage_args+=(--input "$path")
done
CUDA_VISIBLE_DEVICES="" scripts/guarded-hostrun "${stage_args[@]}" -- bash -c '
  set -euo pipefail
  find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
  printf "export GUARD_GIT_HEAD=%s\nexport GUARD_SOURCE_FP=%s\nexport VOICE_TOOLS_CAMPAIGN=voice-barge-20260910-a\n" "$GUARD_GIT_HEAD" "$GUARD_SOURCE_FP" > deploy/source.env
  PYTHONDONTWRITEBYTECODE=1 python3 deploy/check_site.py --require-assets 2>&1 | tail -3
  # var/ is deliberately never promoted, so a bare stage has no notes index and
  # doctor correctly refuses the config. Build it *in the stage* first: that is
  # what makes this a real smoke test of the new code against the new config.
  PYTHONDONTWRITEBYTECODE=1 python3 tools/voicectl.py --config config/host.toml index build
  PYTHONDONTWRITEBYTECODE=1 python3 -u tools/voicectl.py --config config/host.toml doctor
  # Informational only: the live tree still carries the stale ASR profile until
  # step 3 promotes deploy/site_config.py, so compare-live is expected to differ
  # here and must not gate the stage.
  PYTHONDONTWRITEBYTECODE=1 python3 deploy/check_site.py --compare-live 2>&1 | tail -2 || true
'
```

Sources: `scripts/guarded-hostrun:8` (flags), `:46` (relative destination must
end in `/<campaign>`), `:61` (transport), `:80` (source identity exports),
`:82` (remote CPU guard), `:98` (evidence retrieval);
`deploy/source.env.example:1` and `deploy/start-tools-bridge.sh:17` (identity
file), `deploy/site_config.py:153` (campaign override).
The unused-path test was run read-only. The source-file loop binds HTML/JS
and documentation explicitly: `scripts/worktree-fingerprint:22` does not
include those extensions in its source fingerprint. Review the stage manifest
and log under local `evidence/` before promotion.

The payload's last two lines are the gate that matters and the one that was
missing when this section was first run: `doctor` reads `config/host.toml`, and
`var/` is deliberately never promoted, so a bare stage has no notes index and
`doctor` refuses it. Building the index inside the stage is what turns "does the
new code understand the new config" into a question that can actually be answered
before anything on the host changes.

### 3. Stop only the CPU bridge, snapshot it, and copy the staged app

The following is an operator composition of the existing guard/rsync commands,
not a shipped transactional deploy script. `set -e` stops on failure; it does
not perform an automatic rollback. Keep the session output. If the snapshot
fails, the live files are still untouched; bring back only the old CPU bridge.
Once the snapshot finishes, any copy/index failure requires step 6.

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'bash -se' <<'HOST'
cd "$HOME/qwen36/voice-stage/voice-barge-20260910-a"
source deploy/source.env
./.guarded-run --campaign voice-barge-20260910-a --label install \
  --cpu --cpus 24-27 --nice 10 \
  --input web/chat.js --input web/chat.html --input tools/speech_ui.py \
  --input tools/turn_control.py --input config/host.toml -- bash -c '
    set -euo pipefail
    live="$HOME/qwen36/voice-stack/voice-tools-20260910"
    test ! -e rollback
    systemctl --user is-active voice-stack-gpu2.service
    systemctl --user --job-mode=ignore-dependencies stop voice-tools-bridge.service
    mkdir rollback
    rsync -a "$live/" rollback/
    touch rollback-complete
    rsync -aR web tools deploy scripts config README.md ARCHITECTURE.md RUNBOOK.md TOOLS.md PROVENANCE.md tests/README.md .guarded-run "$live/"
    cd "$live"
    python3 tools/voicectl.py --config config/host.toml index build
    python3 tools/voicectl.py --config config/host.toml doctor
  '
HOST
```

Sources: `scripts/guarded-run:11` and `:165` (flags/CPU environment);
`scripts/guarded-hostrun:66` and `:69` (rsync transport);
`tools/voicectl.py:145` (doctor/index CLI). The fresh snapshot is outside the
live tree, in the stage's `rollback/`; `rollback-complete` distinguishes a
finished copy from an interrupted backup. Only the named source paths are
promoted, so live `var/` survives; the notes index is deliberately rebuilt.
The current `config/host.toml:41` adds directory-request approvals, described
in `TOOLS.md`. Do not mistake that capability change for an audio-only update.

The existing installed tools unit and start wrapper matched the repository's
SHA-256 at inspection. No unit installation or daemon reload is needed here.
Do not launch `deploy/start-tools-bridge.sh` beside systemd: its real command
is `.guarded-run --campaign "$campaign" --label bridge-start --cpu --cpus
"$cpus" --nice "$nice" -- python3 -u tools/speech_ui.py --host 0.0.0.0 --port
"$http" --https-port "$https" --tls-cert "$cert" --tls-key "$key" --ffmpeg
"$ffmpeg" --asr-url http://127.0.0.1:8095 --llm-url http://127.0.0.1:8080
--tools-config config/host.toml` (`deploy/start-tools-bridge.sh:22`).
Systemd will invoke it once in step 4.

### 4. Bring back only the CPU bridge

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'bash -se' <<'HOST'
cd "$HOME/qwen36/voice-stage/voice-barge-20260910-a"
source deploy/source.env
./.guarded-run --campaign voice-barge-20260910-a --label restart \
  --cpu --cpus 24-27 --nice 10 -- \
  systemctl --user --job-mode=ignore-dependencies restart voice-tools-bridge.service
HOST
```

Sources: `PROVENANCE.md:127` (tools-service lifecycle),
`deploy/voice-tools-bridge.service:15` (ExecStart), and the host's installed
`/usr/share/man/man1/systemctl.1.gz`, **decompressed lines 2078–2079**:
`ignore-dependencies` prevents pulling other units into the job. That manual
was read over SSH; this mutating command was not executed. Do not use
`systemctl --dry-run restart`: the host's `systemctl --help` does not support
that dry-run verb.

### 5. Check the delivered bytes, service and actual experience

Repeat the read-only status, GPU PID/start timestamp and TLS health commands
above. The GPU PID/timestamp must match step 1. The tools PID/start timestamp
must be fresh, the bridge must be available on 8094, and configured MCP peers
and retrieval must report ready. This config should expose `request_directory`
after the update, in addition to the earlier tools.

```bash
rsync -aRnic web tools deploy/site_config.py vllm:~/qwen36/voice-stack/voice-tools-20260910/
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'cd ~/qwen36/voice-stack/voice-tools-20260910 && PYTHONDONTWRITEBYTECODE=1 CUDA_VISIBLE_DEVICES="" python3 deploy/check_site.py --compare-live'
```

These exact read-only commands were run for the preflight. After deployment
expect no source content differences (no `c`/`s` or `+++++++++` changes;
timestamp-only entries are not byte differences) and a passing asset-path
comparison. A rsync zero exit by itself is insufficient. Hard-refresh
`https://192.168.228.113:8094/chat` so the tab loads the new JS, then perform
the 30-second manual check. Preserve its result with the stage/install/restart
evidence. A green site check does not prove microphone echo handling.

### Executed: 2026-09-10, campaign `voice-barge-20260910-a`

Steps 1–5 were run in order from head `9e7fa48`, source fingerprint
`544d77803f3d0f257fb9f4d1ac6cc89c89f7ff99ef573d8dba9d36daa13e8967`. Every
host-writing step went through the CPU guard on `--cpus 24-27 --nice 10`; no
command in this campaign named a GPU.

| Guard label | Result |
|---|---|
| `stage` | **FAIL** — `doctor: retrieval is enabled but var/notes.sqlite does not exist` |
| `stage2` | PASS — 105 bound inputs exist, index 6 docs / 104 chunks, `doctor: ok`, 4 tools |
| `install` | PASS — snapshot + `rollback-complete`, bytes promoted, index rebuilt, `doctor: ok` |
| `restart` | PASS |

The first failure was the runbook's fault, not the candidate's, and it is the
reason the payload above now builds the index: a stage that has no `var/` can
never satisfy a config whose retrieval is enabled. All four JSON/log pairs are
retained under `evidence/`, the failure included.

Offline gates that ran on these exact bytes before promotion: `make check`
(10 CPU suites, 19/19 site checks), `make sabotage` (every arm correctly
refused), `make sabotage-selftest` (6 arms demonstrated capable of failing),
`make test-browser` (7 suites, chromium + webkit, including the new
`voice_barge_browser` and `voice_carry_browser`).

Post-check, read-only:

- GPU stack **unchanged** — `MainPID=629273`, active since `03:29:25 UTC`,
  identical to the step-1 baseline, and `https://127.0.0.1:8092/chat/health`
  still `available: true`. The 30 GiB stack was never stopped.
- Bridge fresh — `MainPID=714008` since `04:37:16 UTC`, `NRestarts=0`.
- `https://127.0.0.1:8094/chat/health` — **10 tools** (was 9): `request_directory`
  is now offered. Both MCP peers `ready` with 0 restarts. Retrieval
  `ready: true, stale: false`, **104 chunks / 6 documents** (was 60 chunks from
  the older docs).
- Deployed bytes equal the certified bytes: `web/chat.js` is
  `bbe6265e2668145d609b24d018829d7a398df67322dc40ca1cd4923507427f15` — the same
  hash `tests/echo_path_test.mjs` prints when it extracts the gate — and
  `web/chat.html` is `07d08a0645118f77dd118f70d26763d37e8920b431ef7a2373a630b5ee8b7e75`.
- `rsync -aRnic` over `web tools deploy/site_config.py config` and the docs
  reports no content differences, and `check_site.py --compare-live` on the live
  tree now reports **PASS: 20 checks, 0 failures**; the same command was FAIL
  before this campaign, on the stale ASR profile.

Two things this deploy did **not** do:

1. It did not run the 30-second manual microphone check below. Every gate above
   is CPU-only and none of them can hear a room. That check is still owed.
2. It did not promote `Makefile` — step 2 never stages it, so the deployed tree
   keeps an older copy. Cosmetic: no runtime path reads it.

The stage directory `~/qwen36/voice-stage/voice-barge-20260910-a/rollback/` is
the **only** snapshot of the pre-barge-in CPU tree. Do not delete it until the
manual check has passed.

### 6. Roll back this app update if copy, startup or acceptance fails

Use the *new* snapshot from step 3; do not rerun the old ASR campaign.
This restores the CPU tree, including its old config, source identity, notes
index and grants. Any grants/index changes after the snapshot are rolled back.

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'bash -se' <<'HOST'
cd "$HOME/qwen36/voice-stage/voice-barge-20260910-a"
source deploy/source.env
./.guarded-run --campaign voice-barge-20260910-a --label rollback \
  --cpu --cpus 24-27 --nice 10 -- bash -c '
    set -euo pipefail
    live="$HOME/qwen36/voice-stack/voice-tools-20260910"
    test -f rollback-complete
    test -f rollback/deploy/start-tools-bridge.sh
    systemctl --user --job-mode=ignore-dependencies stop voice-tools-bridge.service
    rsync -a --delete rollback/ "$live/"
    systemctl --user --job-mode=ignore-dependencies restart voice-tools-bridge.service
  '
HOST
```

Repeat the same status, GPU PID and TLS health checks; confirm the original
bridge behavior. `--delete` here applies only to the explicit CPU-tree
destination, restoring the complete snapshot and removing newly added modules.
Its actual semantics were read in the host's
`/usr/share/man/man1/rsync.1.gz`, **decompressed line 578**; archive/relative/
dry-run flags are at **522–525 and 567**. The guard and service command sources
are the same as steps 3–4. Rollback has been source-reviewed, not rehearsed.

If step 3 stopped the bridge but did not create `rollback-complete`, it never
reached the source copy. Leave that incomplete snapshot alone and use step 4
to restart the untouched CPU tree. Do not treat it as a valid rollback.

## Existing recovery material on the host

Read-only inspection was:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 vllm 'find ~/qwen36/voice-stack/voice-restore-20260908/rollback ~/qwen36/voice-stack/voice-tools-20260910/deploy/campaigns -maxdepth 3 -type f -print'
```

It found:

- Original stack `rollback/q38-server.txt`, `q38-enabled.txt`,
  `q38-status.txt`: legacy unit/state records, not an app snapshot.
- Tools tree `deploy/campaigns/qasr-20260909/`: `README.md`, `acceptance.py`,
  `deploy.py`, `expected.json`, `negative.py`, `reproduce.py`,
  `run_deploy.sh`, `run_gate.sh`.
- A separate inspection found original-stack
  `evidence/deploy-qasr-20260909/backup/{speech_ui.py,voice_stack.py}` and
  `evidence/deploy-qasr-worker-20260909/backup/`.
- The tools tree has **no `rollback/`**. The exact initial inspection error was
  `ls: cannot access 'rollback': No such file or directory`.

Preserve all of these. The campaign's `restore()` and `stop_stack()` operate
on the GPU stack, and its expected hashes describe a closed historical release
(`deploy/campaigns/qasr-20260909/run_deploy.sh:11`,
`deploy/campaigns/qasr-20260909/deploy.py:264`). They cannot roll back the
current app. The installer now expects `rollback/legacy-unit.txt`
(`deploy/install-service.sh:23`); the host has older `q38-*.txt` names.
Neither the old installer backup nor the ASR backups were restore-tested here.

## When the reply or interruption fails

| Symptom | Check |
|---|---|
| Start does nothing | Use HTTPS and grant microphone access. |
| `barge-note` reports no WebAudio reference | Current `armGlow()` finding above; ordinary playback/button interruption may still work. |
| `barge-note` reports no echo cancellation | The audio track did not report `echoCancellation === true`. Use the button or another supported audio path. |
| Speech during Thinking does nothing | By design, `web/chat.js` only arms barge-in for `phase === 'speaking'`. |
| Four pauses still send half a sentence | `web/chat.js` bounds `fragmentHolds` at 3 holds. |
| Assistant cuts off in a silent room | Record browser/output/volume and disable voice interruption; the scalar `BARGE.echoGain` gate has failed the manual echo check. |
| 503 “conversation model is busy” | One generation holds `chat_lock`; both bridges share the model. |
| Says “One moment.” / “Let me check your notes.” then goes quiet | The reply it was covering came back **empty** (`gen 0 tok` in `run/qwen.log`). The microphone stays live on purpose; ask again. |
| Talks over itself at the start of a reply | By design: the acknowledgment is stopped the instant the reply audio is in hand, so it can be cut mid-word. Switch off “Say something while it thinks”. |
| Text is on screen and the voice lags a second behind it | Regression. The page is asking for the whole answer in one `/tts` request again; it should issue one per sentence. `make test-browser` (`voice_tts_browser`) is the gate. |
| A short breath between sentences of one reply | By design, and it is the fix: each sentence is synthesized separately so the first word arrives in ~0.1 s instead of ~1.2 s. `SPEECH_CHUNK.minChars` in `web/chat.js` merges more of them if it reads as choppy. |
| The orb dims, or interruption stops working, between two sentences | Regression. `playReply` must be called with `final = false` for every clip but the last, or the glow and the barge-in gate are torn down at each seam. |
| Only the first sentences of a long reply are spoken | A later `/tts` request failed. The words are still all on screen and the microphone stays live on purpose; check the Kokoro service. |
| Says “One moment.” and immediately talks over itself | Expected when the first sentence synthesizes faster than the acknowledgment plays. It is cut at the last moment before real speech, not when the answer merely exists. |
| Says “One moment.” on every reply | It should not. The line is armed on a 900 ms deadline and on a real tool event only; if it fires on fast turns the deadline is being charged to turns that earned none. |
| Assistant stopped answering and the page went silent | Check `run/qwen.log` for `gen 0 tok`. Since `voice-barge-20260910-a`+ this is survivable; before it, one empty completion ended the session. |
| 503 STT unavailable | Inspect existing stack logs/health; an app update is not permission to restart the GPU service. |
| Audio stops but next turn waits | The resident upstream may finish its bounded generation despite client cancellation. |
| Tools service is up but new behavior is absent | Compare deployed file bytes, service start time, target port and browser cache. |

The GPU stack's original startup certification is separate:
`deploy/voice_stack.py` verifies pins/releases, starts its processes and runs
a real speech round trip before writing `run/ready` and `run/acceptance.json`.
A tools-bridge restart neither reruns nor renews that GPU acceptance. Its own
long-running `bridge-start` guard finishes its manifest when the process exits
(`scripts/guarded-run:256`); do not expect a completed startup manifest merely
because the CPU service is active.
