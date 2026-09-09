# voice-assistant.  Everything here is offline: no GPU, no model, no network.
# Deployment lives in RUNBOOK.md, not in a make target that could restart a
# 30 GiB stack by habit.
PYTHON ?= python3
# The CPU suites refuse to run unless the variable is set AND empty: an unset
# device is not the same claim as a deliberately emptied one.
CPU = CUDA_VISIBLE_DEVICES=""
NODE   ?= node
PLAYWRIGHT ?= /tmp/kokoro-playback-browser/node_modules/playwright/index.mjs

.PHONY: help test test-bridge test-chat test-browser sabotage check check-site fingerprint inputs

help:
	@printf '%s\n' '  test          bridge + adapter contracts (CPU, no model)' \
	                '  test-browser  the four page suites, chromium + webkit' \
	                '  sabotage      the paired negatives; each must FAIL' \
	                '  check-site    does deploy/site_config.py still describe one real deployment?' \
	                '  check         everything that runs offline' \
	                '  inputs        the asset list a guarded start binds with --input'

test: test-bridge test-chat

test-bridge:
	@test -z "$${CUDA_VISIBLE_DEVICES-}" || { echo "rerun with CUDA_VISIBLE_DEVICES=''"; exit 2; }
	@test -z "$${CUDA_VISIBLE_DEVICES-}" || { echo "CUDA_VISIBLE_DEVICES is set to a device; refusing"; exit 2; }
	$(CPU) $(PYTHON) -u tests/speech_ui_test.py

test-chat:
	$(CPU) $(PYTHON) -u tests/voice_chat_test.py

test-browser:
	@for suite in voice_chat_browser voice_controls_browser voice_language_browser stt_browser; do \
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
	if $(CPU) PLAYWRIGHT="$(PLAYWRIGHT)" "$(NODE)" tests/browser/voice_controls_browser.mjs --sabotage >/dev/null 2>&1; then \
	  echo "SABOTAGE PASSED (this is the failure): keyboard handler"; failures=$$((failures+1)); \
	else echo "ok  correctly refused: voice_controls_browser.mjs --sabotage"; fi; \
	exit $$failures

check-site:
	$(CPU) $(PYTHON) -u deploy/check_site.py

check: test check-site

fingerprint:
	@scripts/worktree-fingerprint .

inputs:
	@$(PYTHON) deploy/voice_stack.py inputs
