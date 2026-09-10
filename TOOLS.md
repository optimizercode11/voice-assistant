# Tools, RAG and MCP

The assistant can look things up.  This file says what it can reach, who said
so, and what happens when a lookup goes wrong.

Nothing here is on by default.  A deployment with no config file offers no
tools and behaves exactly as it did before this existed.

```
   "what port is the bridge on?"
        │
        ▼
   browser ──POST /chat/completions──▶  ┌──────────── bridge (server-side) ────────────┐
        ▲                              │ 1. ask Qwen, offering the tool specs          │
        │  status: "Looking that up…"  │ 2. it answers <tool_call> → run the tool     │
        │  tool: search_notes · 12ms   │ 3. feed the result back as role:"tool"        │
        │  answer + citations          │ 4. ask again; last round has no tools         │
        └─────── NDJSON stream ────────┘   so it must answer                          │
                                       └───────────────────────────────────────────────┘
```

## The rule that shapes everything else

**The browser never supplies a tool result.**  The whole loop happens inside one
HTTP request, on the server.  A tab that could write `{"role":"tool"}` into the
history could make the model believe anything and then say it out loud — that is
not a tool, it is a prompt injection with a Content-Type.

`tests/tool_loop_test.py --sabotage` proves the gate holds: relax
`parse_messages` to trust client roles and exactly one test fails.

Corollaries:

- The page sends only `user`/`assistant` text, exactly as before.  Roles are
  positional, so a tab cannot prefill the assistant or replace the system prompt.
- What the page gets back is *progress* (`{type:'status'}`, `{type:'tool'}`) and
  one `{type:'answer'}`.  Progress is display data.  It is never fed back in.
- The final-answer rules did not move: non-empty, no `think` tags,
  `finish_reason == "stop"`, ≤384 tokens.  `tool_calls` is legitimate only
  *mid*-loop; ending a turn on one is a bug and is reported as one.

## Enable it

```bash
voicectl doctor      --config config/assistant.toml     # what will the model be able to do
voicectl index build --config config/assistant.toml     # build the RAG index
voicectl tools list  --config config/assistant.toml     # exact specs the model will see
voicectl mcp list    --config config/assistant.toml     # start each server, show its tools
```

`voicectl` is `python3 tools/voicectl.py` — symlink it, or `alias voicectl=…`.
`--config` works on either side of the subcommand, and `VOICE_TOOLS_CONFIG` names
the file when neither gives one.

Then start the bridge with `--tools-config config/assistant.toml`.  A config it
cannot understand is a refusal to start, not a warning: `[limitts]`,
`enabled = "yes"`, or `fetch` enabled with an empty allow-list all stop the
process rather than guess.  `VOICE_TOOLS_CONFIG` names the file by environment.

At the deployment, `site_config.py:TOOLS_CONFIG` stays empty until someone names
a file there — so a deploy cannot accidentally start offering tools that
`PROVENANCE.md` never certified.

## Tool calling

The engine (`q38_27_server`) speaks OpenAI tool calling natively, so there is no
prompt emulation: `tools[]` goes into the template verbatim, and a call comes
back as `finish_reason: "tool_calls"` with `message.tool_calls[]`.  Follow-ups
are an assistant message carrying `tool_calls` plus `{"role":"tool",
"tool_call_id":…}` messages.

Three decisions worth their cost:

**Arguments are validated against the tool's own JSON Schema before the tool
runs.**  `tools/agent_tools.py::validate` implements the useful subset —
`type`, `required`, `enum`, `min/max`, `minLength/maxLength`, `pattern`,
`items`, `properties`, `min/maxItems` — and ignores keywords it does not
(`$schema`, `additionalProperties`), because MCP servers ship schemas full of
them.  A model that writes `{"zone": 3}` for `{"zone": ["UTC","Asia/Kolkata"]}`
is stopped before someone else's program is called.

**Errors go back to the model, not to the browser as a 500.**  `"there is no
tool called 'search_note'. Available: now, search_notes"` is a normal turn: the
model fixes it and continues.  A 500 would throw away the generation that just
cost the most.

**The last round is offered no tools.**  `limits.rounds` counts generations, and
the final one has nowhere to send a call, so answering is the only way out.  A
loop that keeps offering tools can spend the whole turn calling them and still
have nothing to speak.

## RAG

`tools/retrieval.py` — SQLite **FTS5**, so real BM25 with no numpy, no model
download, no network.  `/mnt` has 5 GB free and neither machine has numpy; an
embedding stack is not available here, and "which paragraph of *my* notes says
the port" is a lexical question anyway.

- Ingest: `.txt` `.md` `.jsonl` `.ndjson` `.csv` `.tsv`, ~1200-char chunks with
  200-char overlap, split on paragraph breaks, markdown headings carried with
  the chunk so a citation reads `deploy.md#chunk0 · Ports`.
- Query: every word becomes a quoted prefix term, so `NEAR(port bridge)`, `a:b`
  or an unbalanced quote are words, not FTS5 syntax that throws.
- The index is derived, never authoritative.  `status()` compares a SHA-256
  manifest of the corpus against what is on disk and reports `stale`, `added`,
  `changed`, `age_seconds`.  Stale is a warning the model is told about; a
  *missing* index means the tool says "notes are unavailable" rather than
  inventing an answer.
- Rebuild is a full rebuild.  It takes milliseconds at this scale and cannot
  leave a half-merged index behind.

The honest limit: this is lexical.  A paraphrase sharing no word with the source
will miss.  `search_notes` returns "Say that you could not find it rather than
guessing" on a zero-hit query.

## MCP

`tools/mcp_client.py` — the protocol directly, because the `mcp` package is not
installed and stdio MCP is newline-delimited JSON-RPC 2.0: `initialize` →
`notifications/initialized` → `tools/list` → `tools/call`.

An MCP server is **someone else's program that gets to name its own tools**, so:

| Threat | What the client does |
|---|---|
| Renames its tools after listing them | The list is captured **once** at startup and never re-read mid-turn. `tests/fixtures/fake_mcp_server.py renames` proves it. |
| Names a tool the parser would reject | `mcp__<server>__<tool>`, non-`[A-Za-z0-9_-]` folded to `_`, truncated to 64. |
| Offers a destructive tool | `allow` / `deny` in the config, applied before the model ever sees a name. |
| Wedges on a call | Per-RPC timeout, then the call is abandoned; the child is SIGTERM'd, its process group, then killed only if that failed. |
| Crash-loops | Bounded restarts (`MAX_RESTARTS = 2`), then it stays down and says so. |
| Reads your credentials | The child gets `PATH`, `HOME`, `LANG`, `LC_ALL`, `PYTHONPATH`, `NODE_PATH`, `VIRTUAL_ENV` plus whatever `env` you name. Nothing else. |
| Outlives the bridge | `SIGTERM` exits **0** via the same shutdown path as a clean exit, and that path reaps every child. The supervisor stops the bridge with `terminate()`, so dying by signal would leave someone else's server alive holding a pipe to a dead parent. `BridgeStartTests` reads the child's pid from `/tools` and insists it is gone. |
| Leaks a descriptor per restart | `Popen.close()` does not exist in this Python, so pipes are closed by hand and the reader joined first; `make test-mcp` runs under `-W error::ResourceWarning`. |

A server that will not start is a **note in `doctor`, not a crash**: the
assistant comes up with its remaining tools and `doctor --probe` exits non-zero
so a deployment cannot miss it.

## `fetch_url`

Off unless `enabled = true` **and** `allow_hosts` is non-empty — an empty
allow-list is a refusal to start, not an open door.  The allow-list is re-checked
at every redirect, so a `302` cannot walk out of it.  Text content types only,
`max_bytes` bounded, credentials and URL fragments refused.

## Progress, and why it is opt-in

A turn with tools takes several generations, so the page would otherwise stare at
"Thinking" for ten seconds.  A client that sends `Accept: application/x-ndjson`
gets chunked NDJSON:

```
{"type":"status","phase":"tool","round":1,"calls":["search_notes"]}
{"type":"tool","name":"search_notes","ok":true,"ms":12,"source":"retrieval","citations":[{"path":"…/deploy.md","heading":"Ports"}]}
{"type":"answer","text":"The bridge is on 8092.","usage":{…},"tools":[…],"sources":[…]}
```

Everyone else keeps getting one JSON object, unchanged.  The page only asks when
`/chat/health` says `streaming: true` *and* names at least one tool, and it
branches on the **response** Content-Type, so a server that ignores the header
still works.

Citations stay in the transcript after the reply, because "which of my notes said
that" is the question a person asks next.  They are rendered with
`textContent`, never as HTML — `voice_tools_browser.mjs` asserts that a citation
path containing `<img onerror=…>` stays inert.

## Timeouts

| Budget | Default | What it bounds |
|---|---|---|
| `limits.generation_seconds` | 45 s | one trip to the model |
| `limits.per_call_seconds` | 10 s | one tool call, then abandoned and the model told |
| `limits.turn_seconds` | 150 s | the whole turn, generations and tools together |
| `limits.rounds` | 2 | generations; the last has no tools |
| `mcp.server.timeout_seconds` | 15 s | one JSON-RPC call to that server |

A tool that ignores its deadline is *abandoned*, not joined: there is no safe way
to stop someone else's blocking call.  Its thread is a daemon and its result is
discarded, so a hung MCP tool costs the turn its patience and nothing else.

## What is deliberately not here

- **No write-capable builtins.**  No file writes, no shell, no device control.
  Anything that can *do* something arrives as an MCP server you named, with an
  allow-list.
- **No embeddings, no vector store, no reranker.**  Not available offline here,
  and it would be a dependency rather than a capability.
- **No autonomous multi-turn agents.**  One user turn, a bounded number of
  generations, one spoken answer.
