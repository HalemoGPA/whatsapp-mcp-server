# Working on this repo

Read this before changing anything. It is short on purpose.

## The one idea

**WhatsApp enforces almost everything client-side. The display is not the truth.**

"View once", "delete for everyone", poll selection caps, forwarding labels - these
are conventions maintained by the app's UI, not guarantees enforced by WhatsApp's
servers. The app bets that what people see IS reality, and for almost everyone
that bet is correct. It is not correct for us: we read the wire.

The practical consequence, which cost a full day on 2026-07-16:

> A rendering is evidence of what a client chose to draw. Nothing more.

Every wrong conclusion that day came from trusting something that renders:

| Trusted | Said | Truth |
|---|---|---|
| WhatsApp Desktop | "only once - view from your phone" | The message was NOT view-once. Desktop reads the envelope; mobile enforces the inner `viewOnce` flag, which was unset. Fake view-once for weeks. |
| A code comment | "media bytes are downloadable regardless of view count" | True when written (Aug 2024), false since Meta's server-side fix (~Nov 2024). Repeated as fact without checking. |
| whatsmeow's docs | `selectableOptionCount` is "the maximum number of selections" | No client enforces N>1. Library docs describe the field's intent, not client behaviour. |
| Our own docstring | "single-select polls will ignore extras" | They do not. 3 options on a cap-2 poll register all 3. |
| A poll tally in the app | option counts | Five phones showed five different tallies (1/3/3 vs 2/3/2) while the wire said 5/5/5. |

The wire never lied. Every single time, the fix was to go look at the actual
protobuf, the actual stanza, or run an actual test.

## Rules that follow from it

1. **Cite the wire, the proto field number, or a test you ran.** Not a doc, not a
   comment, not a screenshot. If you cannot, say "unverified" and mean it.
2. **A proto field existing does not mean it works.** The server may reject it and
   the recipient's client may ignore it. Those are three different claims. Keep
   them separate. See `docs/protocol-vs-app.md`.
3. **Never repeat a comment in this repo as fact.** They rot. The two most
   confidently wrong statements of 2026-07-16 were both comments in this codebase
   that had been true when written.
4. **Do not claim it works until you have watched it work.** "Compiles",
   "deployed", and "the endpoint returned success" are not "works". A poll vote
   returned `Vote cast` while the tally silently stayed at 0.
5. **Phone-origin bugs are invisible to MCP-side testing.** Sending through our own
   API exercises code that hashes/wraps locally and never decrypts or normalises.
   Two real bugs (all six poll-creation variants; LID-vs-PN on votes) were found
   within minutes by the user's phone after our own tests passed clean. **Close the
   loop with a real phone before claiming a receive path works.**
6. **When the user's observation contradicts your model, they are probably right.**
   "MacBook says only once, phone views forever" was reported three times and
   explained away twice before it was recognised as the bug it was. The user can
   see the one surface we cannot.

Full deploy/build/rollback steps: see docs/DEPLOY.md.

## The box has a hard resource budget - stay inside it

This runs on a **small shared VPS: ~4GB RAM, 2 vCPU, no GPU**, alongside other
containers. Typical free RAM is ~1.5GB and swap is usually partly in use. It is
NOT a spare-capacity machine. An early unguarded `docker compose build` OOM'd it
and took sshd down with it.

Before anything heavy, check `free -m` and `/proc/loadavg`. Rules that follow:

- **Every container must carry `mem_limit` (and ideally `cpus`).** Ours: mcp and
  bridge 512m each. An unbounded container can starve the whole box.
- **No GPU and little spare RAM, so NO local ML models.** This is why voice
  transcription is a CLOUD API (Speechmatics/Groq), not local Whisper -
  whisper-large won't fit and would thrash swap. Reach for a hosted API before a
  local model, always.
- **Prefer I/O-bound designs over CPU/RAM-bound ones.** The parallel transcription
  worker runs 3 concurrent jobs but costs ~13% CPU and <100MB, because the work
  is on Speechmatics' servers - we only wait on HTTP. Concurrency that offloads
  compute is cheap here; concurrency that does compute locally is not.
- **Builds go through `safebuild`** (preflights headroom, memory-caps the build).
  Never bare `docker compose build`.
- **After a change, verify you did not push the box:** `docker stats --no-stream`
  (your container near its mem_limit?) and `free -m` (avail dropping, swap
  climbing?). If a feature needs more than the box has, it does not belong here as
  written - bound it or offload it.

## Repo-specific traps

- `with sqlite3.connect(...) as c` scopes a TRANSACTION, not the connection. It
  does not close. The handle sits in a reference cycle that only the cyclic GC
  frees, and `server.py` raises the gen-0 threshold to 100k allocations, so an
  idle thread leaks forever. This exhausted RLIMIT_NOFILE and took the server down
  silently for 18h. Use the `_conn()` context manager in `scheduling.py`.
- **Docker does NOT restart unhealthy containers**, only exited ones. That is why
  the outage lasted 18h while `docker ps` said "Up". The `autoheal` sidecar does
  it; containers must carry `autoheal: "true"`.
- **Build with `safebuild <service>`**, never bare `docker compose build`. It
  preflights the box and runs the build on a memory-capped buildx builder. (The
  old version's cap was a no-op - it capped the CLI, not BuildKit. Fixed
  2026-07-16; see `deploy/safebuild`.)
- The bridge **must** build with `-tags sqlite_fts5` or it crash-loops on the FTS5
  virtual table.
- **Normalise `@lid` -> phone-number JID** with `resolveToPN` / `resolveToPNStr` on
  anything from an event. Messages from the user's own phone arrive LID-addressed.
  Forgetting this in new code silently breaks joins - no error, just wrong counts.
- Any concurrency-relevant change needs **10+ parallel calls** to test. A
  sequential probe reuses one pool thread and misses thread-bound bugs.
- `@mcp.tool` returns the **plain function** in fastmcp 3.x. There is no `.fn`.

## Ethics that actually apply here

This is the owner's own account and own message history; recovering their own data
is unremarkable. The line worth keeping: capabilities that **deceive a recipient**
(faked link previews, forged quoted context) are different in kind from
capabilities that **read what was already delivered to us**. Both are technically
available. Only one is the user's own data to read. Name which one you are doing.

## Tool-definition token budget (progressive disclosure)

MCP tool definitions are injected into the model's context on EVERY request,
used or not. The full ~92-tool set is a fixed ~17-20k-token tax. We keep it small
WITHOUT dropping capability:

- **`WHATSAPP_MCP_TOOLSET=minimal` (default)** serves 28 hot-core tools directly
  (~7k tokens) plus two meta-tools, **`find_tool`** and **`call_tool`**
  (`toolsearch.py`). The other 64 tools are pruned from the served set but stay
  reachable: `find_tool(query)` ranks the full 92-tool library and returns
  name+description+param-signature; `call_tool(name, arguments)` dispatches to any
  of them. So always-on cost is ~7.4k with 100% capability coverage on demand.
  `WHATSAPP_MCP_TOOLSET=full` loads all 92 directly (no meta-tools).
- `_CORE_TOOLS` (in `server.py`) is the hot core. The full library is captured
  (`toolsearch.capture`) BEFORE the prune, so removed tools remain callable.
- **`call_tool` dispatches INSIDE the process, past the on_call_tool middleware**,
  so `toolsearch.py` re-applies what that chain would have: per-tool scope
  enforcement (mirrors `AuthzMiddleware`) and audit logging of mutating calls
  (mirrors `ObservabilityMiddleware`, tagged `via:call_tool`). Never let it become
  a scope-bypass or an audit hole - if you add a middleware side effect that must
  hold for every tool call, mirror it there too.
- **Optional semantic retrieval** (`embeddings.py`, dormant by default). find_tool
  ranks lexically (IDF + stemming + synonym aliases); on adversarial slang the
  independent eval caps at ~75% recall@8. Set any one of `VOYAGE_API_KEY` /
  `OPENAI_API_KEY` / `GEMINI_API_KEY` (compose passes them through) and find_tool
  auto-upgrades to HYBRID lexical+semantic: the 92 tool docs are embedded once
  (cached to `/var/log/wamcp/tool_embeddings.json`, re-embedded only on doc
  change), each query is embedded per call, and cosine sim is blended with the
  lexical score (`WHATSAPP_MCP_EMBED_WEIGHT`, default 0.6 semantic). No key ->
  pure lexical, zero behaviour change (verified). This is the "hosted API over
  local model" rule again - the box has no GPU for local embeddings.
- `_collapse_optional_schemas()` rewrites FastMCP's verbose `anyOf:[X,null]` unions
  (its rendering of `Optional[...]`) down to the real branch. Pure token savings,
  no behaviour change (validation uses the Python signature, not this JSON).
- Response bodies are already lean (messages are compact text lines, list tools
  return small dicts; `list_chats` ~60 tok/chat, and `include_last_message=False`
  trims it further). Blanket response-field trimming was assessed and declined:
  marginal per-call savings for real regression risk. The always-on tool-def tax
  is the thing worth optimising, and the above covers it.
