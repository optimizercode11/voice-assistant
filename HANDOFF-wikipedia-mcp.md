# Handoff: a Wikipedia fact-checking MCP server

Status: **partly superseded, 2026-09-11.** `tools/mcp_web.py` (the `web` MCP
server in `config/host.toml`, see TOOLS.md) now gives the model `search` with a
Wikipedia-opensearch fallback — which resolves `Tarra Rum Pump` → `Ta Ra Rum
Pum`, the regression in Finding 1 — and `read_page` for any public host. The
capability manifest from "Related open work" also shipped (`Registry.manifest()`,
`purpose` per MCP server). A Wikipedia-specific `resolve_entity`/`lookup_entity`
pair is still unbuilt; build it only if a live measurement shows the general
server missing what it would add. Everything below is measured on this
deployment, not assumed. Two findings change the design; read them first.

## The problem

The model confabulates about the world and does not check, because it has
nowhere to check. Measured on the live bridge, asked "What do you know about
Saif Ali Khan?" it credited him with *Dilwale Dulhania Le Jayenge* (Shah Rukh
Khan) and *Kaho Naa... Pyaar Hai* (Hrithik Roshan), and across four runs gave
three different mothers: Amrita Singh (his ex-wife), Shahnaz Sheikh, Shabana
Khan. Truth: Mansoor Ali Khan Pataudi and Sharmila Tagore.

Retrieval over the user's notes cannot help. That corpus is this repo's six
documentation files, so `search_notes` can answer "what port is the bridge on"
and structurally cannot answer anything about the world.

## Finding 1 -- full-text search fails on exactly the input we get

Titles arrive from the ASR garbled. These are transcripts our own stack produced
for "Ta Ra Rum Pum", a real 2007 film:

| ASR returned | `list=search` | `action=opensearch` | `list=prefixsearch` |
|---|---|---|---|
| `Tarra Rum Pump` | **0 results** | **Ta Ra Rum Pum** | **Ta Ra Rum Pum** |
| `Tara Rumpam` | 0 results | verify | verify |
| `Tara Rumumpam` | 0 results | verify | verify |

`action=query&list=search` is MediaWiki's full-text search and returns **nothing**
for the garbled title. `opensearch` and `prefixsearch` both resolve it.

**An implementation built on `list=search` -- the obvious choice, and the one its
name suggests -- would report "I couldn't find it" for the single case that
motivated this server.** Prefix/autocomplete title resolution is the core
feature, not a refinement. Keep `list=search` only as a secondary
"articles mentioning X".

## Finding 2 -- attaching a tool does not make the model use it

`fetch_url` is enabled for `en.wikipedia.org` as of `321c17d`. Verified live:

    Q: Who are Saif Ali Khan's parents?
      tools: none
      A: Saif Ali Khan's parents are the legendary actors Shabana Khan and Amjad Khan.

Nine tools attached, `fetch_url` among them, prompt explicitly asking it to check
a fact about a real person before stating it. **It called nothing and answered
wrong anyway.** A Wikipedia MCP on its own is likely to be dead weight the same
way.

Two suspects, in order: (a) the model is never told in planning terms what its
capabilities are -- `Tool.spec()` sends schemas, but `TOOLS_PREAMBLE` is generic
prose and no manifest names the servers or their purpose; (b) every generation
runs at `reasoning_effort: "none"` (`voice_chat.py:90`), the mode where it does
not deliberate, and deciding to check *is* a deliberation.

**Do step 0 before writing much code.** Measure whether the model will call any
tool for a factual question, at `none` and `low`, with and without a capability
manifest. If it won't, the real deliverable is the manifest plus the effort
setting and this server is the second half.

## Scope

In: read-only HTTP GET to `en.wikipedia.org`. Out: writes, other hosts, and
account/voice authentication -- the owner is researching a voice authenticator
separately; do not solve it here.

## Tool surface

Two tools, deliberately. One is not enough: the model must be able to *resolve*
before it *reads*.

- `resolve_entity(query)` -> ranked candidate titles from `opensearch` and
  `prefixsearch`. Cheap; the answer to a garbled transcript.
- `lookup_entity(title)` -> REST summary plus sections relevant to the question,
  via `/api/rest_v1/page/summary/<title>`.

Put the resolved title and source URL in the result text so the reply can cite it
in words. The UI already renders `tools` and `sources` per message.

## Hard requirements inherited from this repo

- **Stdlib only.** The host has no node, npm or npx, so official MCP packages
  have no runtime. Every server here is one stdlib Python file; see
  `tools/mcp_files.py` and `tools/mcp_stack_status.py` as the patterns.
- **Read-only absolutely.** GET only, no POST body anywhere, no cookies, no
  credentials. Model supplies a *query string*, never a URL -- otherwise it can
  reach any host the allow-list forgets. Reuse `agent_tools._assert_allowed`
  semantics rather than inventing a second allow-list.
- **Errors go back to the model as `"tool error: ..."`, never HTTP 500.**
- **Rate limits are real.** While probing the API for this document, four rapid
  requests produced HTTP errors. Needs a User-Agent that identifies the app, a
  per-call timeout, one retry with backoff, and a hard per-turn budget.
- **Bound the payload.** Wikipedia summaries are short; section fetches are not.
  Truncate the *payload*, not the serialized JSON -- cutting serialized JSON
  mid-string yields unparseable garbage (`_fit()` in `agent_tools.py` trims the
  payload for exactly this reason).

## Gotchas already paid for -- do not repurchase

- `Popen.close()` does not exist here. Close `stdin/stdout/stderr` by hand and
  join the reader first. `make test-mcp`/`test-files` run under
  `-W error::ResourceWarning`.
- `unittest.main()` must be at the END of the file; strip custom flags from
  `sys.argv` first.
- A sabotage arm must apply to the **subprocess**, not in-process: generate a
  wrapper script that imports the module, patches it, and calls `main()`. See
  `SABOTAGE_WRAPPER` in `tests/mcp_files_test.py`.
- `pgrep -f` matches its own shell command line.
- Test the **TLS listener**, not the plain HTTP port. That is how "TLS listener
  had no registry" shipped past 71 green tests.
- `127.0.0.1` from the dev VM is not the host. Use `ssh vllm 'curl ...'` or
  `https://192.168.228.113:8094`.

## Acceptance gates

Fixtures are the transcripts this stack actually produced, not invented strings.

1. `resolve_entity("Tarra Rum Pump")` returns `Ta Ra Rum Pum`. This is the
   regression that matters most; a green suite without it proves nothing.
2. `lookup_entity("Ta Ra Rum Pum")` reports 2007 and Saif Ali Khan.
3. A query naming no article returns "no such article", not a guess.
4. A host other than `en.wikipedia.org` is refused, **including via redirect**.
5. A dead/slow upstream degrades to a tool error inside the per-call budget; the
   turn still answers.
6. Sabotage arm: point the resolver at `list=search` and assert gate 1 FAILS.
   That is the paired negative for Finding 1 -- it proves the suite can see the
   design error this document exists to prevent.

Run `make check`, `make sabotage`, `make test-browser`, then the host-side
regression through `scripts/guarded-hostrun ... --cpu` before deploying. Restart
only `voice-tools-bridge.service`; **never** `voice-stack-gpu2.service`.

## Related open work

- Capability manifest (`Registry.manifest()`) -- step 0 above, and the blocker.
- `tools/agent_config.py` is dirty with an unused `purpose` field on
  `MCPServerConfig`, left uncommitted deliberately. It is the seed of the
  manifest: one line per server describing what it is for.
- Directory approvals (voice-asks / click-confirms) -- separate design, separate
  security model, not this handoff.
- qasr defects still unfixed and they are the ceiling on ASR accuracy: tail
  degeneration (65 trailing `?` on a clean 2.5 s clip) and degenerate *onset*
  ("About the Ritz Carlton" -> "**B** the Ritz Carlton"). Needs a qasr campaign
  and fresh GPU authorization.
