---
name: add-tool
description: Add a new MCP tool to this WhatsApp server. Use when asked to expose a new capability, wrap a new bridge endpoint, or add a tool to tools.py or scheduling.py. Covers annotations, the confirm guard, core-set placement, retrieval aliases, and the eval case.
---

# Adding an MCP tool

Five steps. Skipping step 3 or 4 produces a tool that exists but that the model
can never find, which is the same as not shipping it.

## 1. Write the tool

Tools live in `whatsapp-mcp-server/tools.py` (general) or `scheduling.py`
(queue, drafts, notes). Register with `@mcp.tool(annotations=...)` using one of
the five constants defined at the top of `tools.py`:

| Constant | For |
|---|---|
| `READ_LOCAL` | Reads the local store. No network, no side effect. |
| `READ_BRIDGE` | Reads via the bridge (touches the network), no side effect. |
| `SEND` | Sends a message or file, non-idempotent. |
| `MUTATE` | Changes state, recoverable (react, mark read, edit). |
| `DESTRUCTIVE` | Irreversible: delete, revoke, leave, block. |

A `DESTRUCTIVE` tool takes `confirm: bool = False` and calls `_require_confirm`
before doing anything:

```python
@mcp.tool(annotations=DESTRUCTIVE)
def delete_thing(thing_id: str, confirm: bool = False) -> dict:
    """One-line summary the model reads.

    Args:
        thing_id: ...
        confirm: Must be True. The user, not the LLM, has to have agreed.
    """
    if (denied := _require_confirm(confirm)) is not None:
        return denied
    ...
```

The annotation is only a hint to the client. The `confirm` kwarg is the actual
enforcement, and it exists because older MCP clients do not support elicitation.

Clamp every caller-supplied bound. The existing tools use `limit <= 100`,
context windows `<= 20`, message body `<= 65536` bytes. Match them.

## 2. Decide whether it belongs in the core set

`_CORE_TOOLS` in `server.py` is the 29-tool hot core served directly in the
default minimal toolset. Every entry there is paid for on **every request to the
model**, whether or not WhatsApp is touched.

The bar: would a typical session use this in the first three calls? If not, leave
it out. It stays fully reachable through `find_tool` / `call_tool`. Most new
tools should not be in the core.

## 3. Make it findable

If the tool's name and description do not share vocabulary with how a person
would ask for it, `find_tool` cannot rank it. `schedule_message` never lexically
matches "send this tomorrow morning".

Add an entry to `_ALIASES` in `toolsearch.py`:

```python
"your_tool": "the words a user would actually say for this thing",
```

Write real phrasings. **Do not copy strings out of the eval cases.** An alias map
tuned against its own answer key measures nothing.

## 4. Add an eval case

Append to `tests/toolsearch-eval/cases.json`:

```json
{"q": "how someone would phrase the task", "gold": "your_tool", "alts": ["a genuinely correct substitute"]}
```

`alts` is for tools that would also be a right answer, not near misses. Then run
the eval and confirm recall did not drop.

## 5. Verify

```bash
pytest
ruff check .
```

Then, against a live server, add a line to `tests/smoke.sh` and run it. A tool
that has never been called over real HTTP is not done.

## If the tool needs a new bridge endpoint

Add the handler in `whatsapp-bridge/main.go`, then a thin wrapper in
`whatsapp.py`, then the tool in `tools.py`. Keep the wrapper thin: the query and
retry logic belongs in `whatsapp.py`, the MCP-facing shape belongs in `tools.py`.

Rebuild with the FTS5 tag or the bridge crash-loops:

```bash
go build -tags sqlite_fts5 ./...
```
