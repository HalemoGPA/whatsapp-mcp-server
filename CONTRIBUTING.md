# Contributing

Issues and pull requests are welcome. Read [CLAUDE.md](CLAUDE.md) first: it is
short, and it is the reason this project's claims can be trusted.

## The one rule

**A rendering is evidence of what a client chose to draw. Nothing more.**

WhatsApp enforces most of what you see client-side. "View once", "delete for
everyone", poll caps: the app maintains these as conventions, the server does not
guarantee them. So in a PR description or an issue, do not cite a screenshot, a
doc, or a comment in this repo as evidence. Cite one of:

- the wire, or the protobuf field number
- a test you ran, with its output
- an observation from a real phone

If you cannot, write "unverified" and mean it. Every wrong conclusion this
project has recorded came from trusting something that renders.

Corollaries that have each cost real time:

- **A proto field existing does not mean it works.** The server may reject it and
  the recipient's client may ignore it. Those are three separate claims.
- **Do not claim it works until you have watched it work.** "Compiles",
  "deployed", and "the endpoint returned success" are not "works". A poll vote
  once returned `Vote cast` while the tally silently stayed at zero.
- **Phone-origin bugs are invisible to server-side testing.** Sending through our
  own API exercises code that wraps locally and never decrypts or normalises.
  Close the loop with a real phone before claiming a receive path works.
- **Comments rot.** The two most confidently wrong statements in this codebase's
  history were both comments that had been true when written.

## Before you open a PR

```bash
pytest                                          # 95 offline tests
ruff check .
cd whatsapp-bridge && go vet -tags sqlite_fts5 ./...
```

CI runs exactly these plus a secret scan. All three must pass.

`ruff format` is deliberately **not** enforced. Several modules use hand-aligned
constant blocks the formatter would flatten. Match the surrounding style instead.

## Adding a tool

1. Put it in `tools.py` with the right annotation constant (`READ_LOCAL`,
   `READ_BRIDGE`, `SEND`, `MUTATE`, or `DESTRUCTIVE`). Destructive tools take
   `confirm: bool = False` and
   call `_require_confirm` first.
2. Decide whether it belongs in `_CORE_TOOLS` in `server.py`. The bar is high:
   the core is what every request pays for. Most tools should not be there.
3. If the name is not lexically obvious, add an entry to `_ALIASES` in
   `toolsearch.py` so `find_tool` can reach it from plain language. Write the
   aliases as phrasings a user would really use, **not** as copies of any eval
   query.
4. Add a case to `tests/toolsearch-eval/cases.json` and rerun the eval.

## Changing toolsearch

`call_tool` dispatches inside the process, past the MCP middleware chain. If you
add a middleware side effect that must hold for every tool call, mirror it in
`toolsearch.py` too. Scope enforcement and audit logging are already mirrored
there. Letting the dispatcher drift from the middleware turns it into a scope
bypass or an audit hole.

Retrieval changes need a number. Run `tests/toolsearch-eval` before and after and
put both in the PR. "Feels better" is not a result.

## Testing

The offline suite in `tests/unit/` must stay offline: no WhatsApp, no bridge, no
network. That is what lets CI and a fresh clone both run it. If a change is only
testable against a live session, add it to `tests/smoke.sh` instead and say so.

## Resource budget

This is built to run on a small VPS sharing a box with other services. Two rules
follow, and neither is negotiable:

- **Reach for a hosted API before a local model.** Voice transcription is a cloud
  API rather than local Whisper because the target hardware has no GPU and
  whisper-large would thrash swap.
- **Prefer I/O-bound designs.** Concurrency that offloads compute is cheap here.
  Concurrency that does compute locally is not.

Containers carry `mem_limit`. If a feature needs more than a 4GB box has, bound
it or offload it.

## Style

- Explain **why**, not what. The code says what.
- When you fix a bug that cost you time, write down what misled you. Most of the
  comments in this repo exist because something was confidently wrong first.
- No em dashes.
