# Tests

Four suites, split by what they need to run.

| Suite | Needs | Runs in CI |
|---|---|---|
| `unit/` | Nothing | Yes |
| `smoke.sh` | A live server with a paired session | No |
| `toolsearch-eval/` | The full tool library loaded | No |
| `voice-harness/` | Real audio and a transcription key | No |

## `unit/` - the offline suite

```bash
pytest
```

95 tests, no WhatsApp, no bridge, no network. This is the one that gates every
push, so it has to stay offline. If a change is only testable against a live
session it belongs in `smoke.sh`, not here.

**`test_identity.py`** builds a synthetic whatsmeow store (the `whatsmeow_lid_map`
and `whatsmeow_contacts` tables, with the awkward cases: a LID with no name, a
push name that was never saved, a saved name that differs from the push name) and
checks the property that matters: resolution can make attribution more accurate,
never confidently wrong. An unmapped identifier must echo back rather than
resolve to somebody.

**`test_toolsearch.py`** pins the retrieval invariants a tuning change must not
break. Ranking *quality* is measured next door in `toolsearch-eval`; this file
guards the structure: singular and plural land on the same stem, results come
back ordered, a malformed schema renders an empty signature instead of raising,
a missing embedding scores zero instead of crashing, and the meta-tools are never
themselves dispatchable.

That first one caught a real bug. A blanket `es` strip sent `messages` to
`messag` while `message` stayed `message`, so a query saying "message" missed a
description saying "messages", across most of this domain's vocabulary (`file`,
`name`, `image`, `note`).

## `smoke.sh` - every tool over real HTTP

```bash
WHATSAPP_MCP_TOKEN=... bash tests/smoke.sh
```

Calls each tool against a running server and asserts on the response shape,
including that an unauthenticated request is a 401. A tool that has never been
called over real HTTP is not finished.

The JIDs in it are placeholders. Point them at chats that exist on your own
account before running.

## `toolsearch-eval/` - does retrieval pick the right tool

```bash
python tests/toolsearch-eval/eval.py
```

A labeled set: each case is a natural-language task, the gold tool, and any
alternates that would also be correct. Labels are deterministic, so recall@k
needs no LLM judge.

The queries were written independently of the alias map in `toolsearch.py`, and
they need to stay that way. **Do not tune aliases against these strings.** An
eval scored against its own answer key measures nothing.

`build_routing_probe.py` dumps what `find_tool` actually returns for every case,
so a regression shows you the ranking rather than just a lower number.

Lexical-only recall@8 caps around 75% on adversarial slang. Set an embedding
provider key to measure the hybrid path.

## `voice-harness/` - transcription quality

```bash
python tests/voice-harness/run.py
```

Real audio and real transcripts stay local. `audio/` and `results/` are
gitignored and must remain so; they contain other people's voices.
