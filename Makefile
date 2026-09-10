# voice-assistant.  Everything here is offline: no GPU, no model, no network.
# Deployment lives in RUNBOOK.md, not in a make target that could restart a
# 30 GiB stack by habit.
PYTHON ?= python3
# The CPU suites refuse to run unless the variable is set AND empty: an unset
# device is not the same claim as a deliberately emptied one.
CPU = CUDA_VISIBLE_DEVICES=""
NODE   ?= /tmp/kokoro-playback-browser/node_modules/.bin/node
PLAYWRIGHT ?= /tmp/kokoro-playback-browser/node_modules/playwright/index.mjs

.PHONY: help test test-bridge test-chat test-tools test-retrieval test-mcp test-files test-approvals test-barge test-loop test-browser \
        sabotage check check-site doctor fingerprint inputs

help:
	@printf '%s\n' '  test          bridge, adapter, tools, RAG, MCP, loop (CPU, no model)' \
	                '  test-browser  the five page suites, chromium + webkit' \
	                '  sabotage      the paired negatives; each must FAIL' \
	                '  doctor        what the model will be able to do, and what is broken' \
	                '  check-site    does deploy/site_config.py still describe one real deployment?' \
	                '  check         everything that runs offline' \
	                '  inputs        the asset list a guarded start binds with --input'

test: test-bridge test-chat test-retrieval test-tools test-mcp test-files test-approvals test-barge test-loop test-turn

test-bridge:
	@test -z "$${CUDA_VISIBLE_DEVICES-}" || { echo "rerun with CUDA_VISIBLE_DEVICES=''"; exit 2; }
	@test -z "$${CUDA_VISIBLE_DEVICES-}" || { echo "CUDA_VISIBLE_DEVICES is set to a device; refusing"; exit 2; }
	$(CPU) $(PYTHON) -u tests/speech_ui_test.py

test-chat:
	$(CPU) $(PYTHON) -u tests/voice_chat_test.py

test-retrieval:
	$(CPU) $(PYTHON) -u tests/retrieval_test.py

test-tools:
	$(CPU) $(PYTHON) -u tests/agent_tools_test.py

test-mcp:
	$(CPU) $(PYTHON) -W error::ResourceWarning -u tests/mcp_test.py

# The loop needs the TLS bridge and a scripted upstream; it is the one that
# proves a browser cannot write a tool result.
# The filesystem server is adversarially tested, not just functionally: the
# claim is that a symlink inside a root cannot read /etc/passwd.
test-files:
	$(CPU) $(PYTHON) -W error::ResourceWarning -u tests/mcp_files_test.py

test-turn:
	$(CPU) $(PYTHON) -u tests/turn_control_test.py

# Directory grants are the one capability that can widen while the assistant is
# running, so they get their own suite: the claim under test is that the model
# can ask and cannot take.  It spawns a real file server and a real bridge.
test-approvals:
	$(CPU) $(PYTHON) -W error::ResourceWarning -u tests/approvals_test.py

# The barge-in gate decides whether the assistant hears the user or hears
# itself, and a headless browser has no acoustic echo path to test that in, so
# it is tested numerically against the real source instead.
test-barge:
	$(CPU) $(NODE) tests/barge_gate_test.mjs

test-loop:
	$(CPU) $(PYTHON) -u tests/tool_loop_test.py

doctor:
	$(CPU) $(PYTHON) -u tools/voicectl.py --config config/assistant.toml doctor

test-browser:
	@for suite in voice_chat_browser voice_carry_browser voice_barge_browser voice_controls_browser voice_language_browser stt_browser voice_tools_browser; do \
	  echo "== $$suite =="; \
	  $(CPU) PLAYWRIGHT="$(PLAYWRIGHT)" "$(NODE)" tests/browser/$$suite.mjs || exit 1; \
	done

# A gate that cannot fail is not a gate: each of these must exit non-zero.
sabotage:
	@failures=0; \
	# --negative is a control (it must pass); --speaker-sabotage is the
	# paired sabotage (it must fail at exactly one assertion).
	for arm in "--speaker-sabotage"; do \
	  if $(CPU) $(PYTHON) -u tests/speech_ui_test.py $$arm >/dev/null 2>&1; then \
	    echo "SABOTAGE PASSED (this is the failure): speech_ui_test.py $$arm"; failures=$$((failures+1)); \
	  else echo "ok  correctly refused: speech_ui_test.py $$arm"; fi; \
	done; \
	# Each of these is a load-bearing claim: the browser cannot write a tool
	# result, a server cannot rename a tool mid-turn, a file server's
	# containment cannot be reduced to a plain join (that is the arm that
	# leaks /etc/passwd through a symlink) -- and the page cannot drop the half of
	# a sentence it already heard.
	for suite in tool_loop_test mcp_test mcp_files_test turn_control_test approvals_test; do \
	  if $(CPU) $(PYTHON) -u tests/$$suite.py --sabotage >/dev/null 2>&1; then \
	    echo "SABOTAGE PASSED (this is the failure): $$suite.py --sabotage"; failures=$$((failures+1)); \
	  else echo "ok  correctly refused: $$suite.py --sabotage"; fi; \
	done; \
	if $(CPU) PLAYWRIGHT="$(PLAYWRIGHT)" "$(NODE)" tests/browser/voice_tools_browser.mjs --sabotage >/dev/null 2>&1; then \
	  echo "SABOTAGE PASSED (this is the failure): the page ignores tool progress"; failures=$$((failures+1)); \
	else echo "ok  correctly refused: voice_tools_browser.mjs --sabotage"; fi; \
	if $(CPU) PLAYWRIGHT="$(PLAYWRIGHT)" "$(NODE)" tests/browser/voice_barge_browser.mjs --sabotage >/dev/null 2>&1; then \
	  echo "SABOTAGE PASSED (this is the failure): barge-in arms with no echo reference"; failures=$$((failures+1)); \
	else echo "ok  correctly refused: voice_barge_browser.mjs --sabotage"; fi; \
	if $(CPU) $(NODE) tests/barge_gate_test.mjs --sabotage >/dev/null 2>&1; then \
	  echo "SABOTAGE PASSED (this is the failure): the echo floor is decorative"; failures=$$((failures+1)); \
	else echo "ok  correctly refused: barge_gate_test.mjs --sabotage"; fi; \
	if $(CPU) PLAYWRIGHT="$(PLAYWRIGHT)" "$(NODE)" tests/browser/voice_carry_browser.mjs --sabotage >/dev/null 2>&1; then \
	  echo "SABOTAGE PASSED (this is the failure): the page drops a held fragment"; failures=$$((failures+1)); \
	else echo "ok  correctly refused: voice_carry_browser.mjs --sabotage"; fi; \
	if $(CPU) PLAYWRIGHT="$(PLAYWRIGHT)" "$(NODE)" tests/browser/voice_controls_browser.mjs --sabotage >/dev/null 2>&1; then \
	  echo "SABOTAGE PASSED (this is the failure): keyboard handler"; failures=$$((failures+1)); \
	else echo "ok  correctly refused: voice_controls_browser.mjs --sabotage"; fi; \
	exit $$failures

check-site:
	$(CPU) $(PYTHON) -u deploy/check_site.py

# The MCP server the deployed bridge ships with, exercised as a real peer.
mcp-probe:
	$(CPU) $(PYTHON) -u tools/mcp_client.py --command $(PYTHON) --arg tools/mcp_stack_status.py

check: test check-site

fingerprint:
	@scripts/worktree-fingerprint .

inputs:
	@$(PYTHON) deploy/voice_stack.py inputs
