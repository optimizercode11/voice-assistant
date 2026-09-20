# Voice GPU4 deployment — voice-gpu4-20260920-b

User requested voice assistant deployment on GPU4 after q36 shutdown/disable.
Dedicated worktree based on87b05be with reviewed prepared GPU4 changes copied
from /mnt/voice-assistant-gpu4-20260920; original dirty worktree preserved.
Kokoro and Qwen3-ASR1.7B FP8 on GPU4; G2P and tools bridge CPU28-31/nice10;
external q3827 remains GPU2 port8080. Public HTTPS8094/chat retained.
Remote campaign ~/qwen36/voice-stack/voice-gpu4-20260920-b. New labels all use
this campaign; no older campaign labels combined. Own unit voice-stack-gpu4,
old bridge/unit configuration saved for rollback; q36 must remain disabled.
Prepared lifecycle/ownership controls reviewed. Fresh CPU regression, positive,
paired extra-LLM sabotage, legacy conflict reproduce, asset hashes and full
TTS→STT→chat→TTS acceptance required before completion.

COMPLETE 04:45Z. Fresh GPU-bugfix guardrail GREEN, source79cef734942ddb7e.
Native GPU acceptance and CPU install manifests PASS; GPU containment CLEAN,
2 speech PIDs on GPU4, 0 unattributed. StackMainPID3017152 active/enabled/0restarts;
Kokoro3017786=1342MiB, QASR3018513=3938MiB, PGID3017145. q38MainPID2996026
unchanged, q36 inactive/disabled. TLS verified from VM:8094/chat/health available,
16tools, all5MCP servers ready, retrieval6docs/236chunks. Exact fox transcription,
English/Hindi non-silent24kHz audio, contextFour→Five, malformed controls pass.
No hardware microphone/speaker claim. Rollback state captured q36 already disabled.
