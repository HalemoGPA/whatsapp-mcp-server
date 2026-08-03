# whatsapp-mcp-server

[![CI](https://github.com/HalemoGPA/whatsapp-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/HalemoGPA/whatsapp-mcp-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](whatsapp-mcp-server/pyproject.toml)
[![Go 1.25](https://img.shields.io/badge/go-1.25-00ADD8.svg)](whatsapp-bridge/go.mod)

A self-hosted, authenticated **MCP server for WhatsApp**: 96 tools over your own
message history, with server-side tool retrieval so the model pays for 29 of them
instead of 96, and a labeled eval that measures whether retrieval actually picks
the right tool.

Runs as two containers on a small VPS. Your messages stay on your box; the only
credential is a bearer token you generate.

```text
Claude / any MCP client ──HTTPS + Bearer──> nginx ──> mcp container ──> bridge container ──> WhatsApp
```

---

## Why this exists

The reference WhatsApp MCP servers are stdio-only, unauthenticated, and expose a
flat tool list. That is fine on a laptop and unworkable as an always-on service.
Three problems show up immediately, and most of this repo is the answer to them.

**1. Tool definitions are a fixed tax.** MCP injects every tool definition into
the model's context on *every* request, whether or not the request touches
WhatsApp. At 96 tools that is roughly 20k tokens you pay for asking "what time is
it". Dropping tools to save tokens means losing capability.

**2. The same person has two identities.** WhatsApp hides phone numbers behind
LIDs inside groups, so one human appears under their phone number in DMs and
under an opaque LID in group history. Any tool that filters on a single sender ID
silently returns half their messages, with no error to tell you.

**3. The app's UI is not the protocol.** "View once", "delete for everyone", poll
selection caps: these are conventions the client draws, not guarantees the server
enforces. A server that reads the wire will disagree with the app, and when it
does, the app is usually the one that is wrong.

---

## The parts worth reading

### Progressive disclosure: 96 tools, 29 tools' worth of context

`WHATSAPP_MCP_TOOLSET=minimal` (the default) serves 29 hot-core tools directly and
prunes the other 67 from the served set, but captures the full library first so
nothing becomes unreachable. Two meta-tools bridge the gap:

```text
find_tool(query)            -> ranked catalog entries (name, description, param signature)
call_tool(name, arguments)  -> dispatch to any tool in the full 96-tool library
```

| | tools served | always-on cost |
|---|---:|---:|
| `TOOLSET=full` | 96 | ~20k tokens |
| `TOOLSET=minimal` (default) | 29 + 2 meta | ~8k tokens |

No capability is removed and there is no mode to flip at call time. The long tail
is one search away.

**The security consequence is the interesting part.** `call_tool` dispatches
*inside the process*, past the MCP `on_call_tool` middleware chain, so it has to
re-apply everything that chain would have done: per-tool scope enforcement and
audit logging of mutating calls. Both are mirrored in `toolsearch.py` so the
dispatcher can never become a scope bypass or an audit hole. A retrieval layer
that quietly skips your authorization middleware is worse than no retrieval layer.

Retrieval is lexical by default (IDF-weighted, stemmed, with a synonym alias map)
and upgrades to **hybrid lexical + semantic** if any embedding provider key is
present, with no behaviour change when there is none.

### The retrieval is measured, not asserted

`tests/toolsearch-eval/` holds a labeled set of natural-language tasks with a gold
tool and acceptable alternates per case, plus a probe that dumps what `find_tool`
actually returns for each. Labels are deterministic, so recall@k needs no LLM
judge. Queries were written independently of the alias map, and the README there
says so, because an eval tuned against its own answer key measures nothing.

Lexical-only recall@8 caps around 75% on adversarial slang. That number is in the
repo because a retrieval layer without a number attached is a vibe.

### Cross-identity resolution

`identity.py` reads whatsmeow's own `whatsmeow_lid_map` and `whatsmeow_contacts`
tables, read-only, and joins a person's phone number and LID into one identity.
`list_person_messages` then returns their full history across DMs and groups
instead of whichever half matched.

Three properties it holds, in priority order:

- **Read-only.** Never writes, never touches the message store.
- **Additive.** No existing code path imports it; resolution is a lens a caller
  opts into, so existing tools cannot regress.
- **Never wrong.** Anything unmapped resolves to itself. It can make attribution
  more accurate; it can never make it confidently wrong.

### Documented attribution limits

Group messages before a certain date were stored with the *group's* JID as the
sender. The real sender was never recorded and cannot be recovered. The server
reports those as unattributable rather than inferring a sender from context.

Same posture in [`docs/protocol-vs-app.md`](docs/protocol-vs-app.md), which
catalogues where the wire and the app disagree and marks every claim as
`VERIFIED` (tested here), `SOURCE` (read in the protocol) or `SPECULATIVE` (not
confirmed). Several capabilities in there are deliberately **not implemented**:
the wire permits them, and they work by deceiving a recipient rather than by
reading data already delivered to us. That line is the project's one editorial
position.

### Voice notes are searchable text

`transcription.py` runs a background worker that finds voice notes with no
transcript, decrypts the audio, transcribes it through a hosted API, and stores
the result in a **separate writable database**. The message store is mounted
read-only and the bridge writes it with `INSERT OR REPLACE`, so a transcript
column on `messages` would be silently wiped on any resync.

Arabic gets its own normalisation pass: clitics (`ال`, `و`, `ب`, `ل`, `ف`) attach
as prefixes, so plain FTS token matching never matches `فارماسي` against a stored
`الفارماسي`. A folded copy of each transcript handles that plus the spelling
variants people type inconsistently (alef forms, `ة`/`ه`, `ى`/`ي`).

---

## Architecture

```text
                    ┌─────────────────────────────────────────────┐
  MCP client ──────>│ nginx  (TLS, proxy_buffering off for /mcp)  │
  Bearer token      └───────────────────┬─────────────────────────┘
                                        │ 127.0.0.1:9100
                    ┌───────────────────▼─────────────────────────┐
                    │ mcp container   (Python, FastMCP)           │
                    │   token verify + scopes   observability.py  │
                    │   96 tools                tools.py          │
                    │   find_tool / call_tool   toolsearch.py     │
                    │   LID <-> phone           identity.py       │
                    │   CDN decrypt             media.py          │
                    │   voice -> text           transcription.py  │
                    │   send-later queue        scheduling.py     │
                    └───────────────────┬─────────────────────────┘
                                        │ http://bridge:8080  (private network)
                    ┌───────────────────▼─────────────────────────┐
                    │ bridge container  (Go, whatsmeow)           │
                    │   holds the WhatsApp session                │
                    │   writes messages to SQLite (FTS5)          │
                    │   REST API, never published publicly        │
                    └─────────────────────────────────────────────┘
```

- The bridge is `expose:`-only. It is reachable from the mcp container and from
  nowhere else.
- The mcp server publishes on `127.0.0.1` only; nginx is the sole public path.
- Auth is enforced **in the application**, not just at the proxy, so a proxy
  misconfiguration cannot open the server.

### Modules

| File | Lines | What it does |
|---|---:|---|
| `whatsapp-bridge/main.go` | ~5,800 | whatsmeow session, SQLite ingest, internal REST API |
| `whatsapp-mcp-server/whatsapp.py` | ~2,550 | Query layer over the message store |
| `tools.py` | ~1,530 | The 86 general tools, with MCP annotations and clamps |
| `toolsearch.py` | ~630 | Tool retrieval, dispatch, scope + audit mirroring |
| `transcription.py` | ~580 | Voice-note worker, key failover, Arabic normalisation |
| `media.py` | ~520 | CDN fetch, HKDF + AES-CBC decrypt, signed download links |
| `server.py` | ~440 | App assembly, token/scope verification, toolset pruning |
| `scheduling.py` | ~420 | Send-later queue, drafts, chat notes |
| `observability.py` | ~240 | Prometheus metrics + append-only JSONL audit log |
| `identity.py` | ~175 | LID <-> phone resolution |
| `embeddings.py` | ~155 | Optional semantic retrieval, any of three providers |
| `prompts.py` / `resources.py` | ~175 | MCP prompts and resources |

---

## Tools

96 tools. In the default minimal toolset the **29 bold** ones are served directly;
the rest are reachable through `find_tool` / `call_tool`.

| Group | Tools |
|---|---|
| **Search / read** | **`search_contacts`** · **`search_all_messages`** · **`list_messages`** · **`list_chats`** · **`get_chat`** · **`get_message_context`** · **`get_direct_chat_by_contact`** · `get_contact_chats` · `get_last_interaction` · `list_chats_by_state` · `export_chat` |
| **Send** | **`send_message`** · **`reply_to_message`** · **`send_file`** · **`send_audio_message`** · **`send_view_once_media`** · **`forward_message`** · `send_location` · `send_sticker` · `send_contact_card` · `mention_everyone` |
| **Media** | **`save_media`** · **`view_media`** · **`list_media_in_chat`** · **`list_all_media_by_type`** · **`list_view_once`** · **`get_media_info`** |
| **Voice** | **`transcribe_voice`** · **`search_voice_notes`** · `list_nonspeech_voices` |
| **Polls** | **`create_poll`** · **`vote_in_poll`** · **`get_poll_results`** |
| **Message ops** | **`react_to_message`** · **`mark_read`** · **`edit_message`** · **`delete_message`** · `star_message` · `set_disappearing` · `send_presence` · `get_message_receipts` · `get_message_thread` · `get_replies_to` · `get_reactions_on` · `search_by_reaction` |
| **Identity** | `resolve_identity` · `list_person_messages` · `sender_activity` · `check_phones_on_whatsapp` · `get_user_info_bulk` |
| **Scheduling** | `schedule_message` · `cancel_scheduled` · `list_scheduled` · `save_draft` · `list_drafts` · `send_draft` · `delete_draft` · `set_chat_note` · `get_chat_note` · `list_chat_notes` |
| **Groups** | `create_group` · `leave_group` · `list_group_members` · `update_group_participants` · `get_group_info` · `get_group_invite_link` · `join_group_by_link` · `groups_with_member` · `list_joined_groups` · `backfill_group_participants` |
| **Chat state** | `mute_chat` · `pin_chat` · `archive_chat` · `delete_chat` · `mark_chat_unread` · `set_chat_label` · `set_message_label` · `edit_label` |
| **Analytics** | `count_messages` · `messages_by_day` · `get_chat_stats` · `get_top_active_chats` · `get_quiet_chats` |
| **Profile / privacy** | `get_profile_picture` · `get_privacy_settings` · `set_status_message` · `post_status` · `get_business_profile` · `block_user` · `unblock_user` · `get_blocklist` |
| **Newsletters** | `list_subscribed_newsletters` · `get_newsletter_info` |
| **Ops** | **`bridge_health`** · `get_bridge_stats` · `get_bridge_diagnostics` |
| **Meta** | `find_tool` · `call_tool` |

Read tools are cached for 30s per token. Mutating tools are never cached.
Destructive tools require an explicit `confirm=True` that the model must produce
on purpose, because older MCP clients do not support elicitation and an
annotation alone is only a hint to the client.

---

## Quick start

Requires Docker, a domain with TLS, and a phone with WhatsApp.

```bash
git clone https://github.com/HalemoGPA/whatsapp-mcp-server.git
cd whatsapp-mcp-server

printf 'WHATSAPP_MCP_TOKEN=wamcp_%s\n' "$(openssl rand -hex 32)" > .env
chmod 600 .env

docker compose up -d --build
curl -fsS http://127.0.0.1:9100/health          # {"ok": true}
```

Pair the phone once. The QR refreshes about every 20 seconds, so be ready:

```bash
docker compose logs -f bridge     # "Scan this QR code with your WhatsApp app:"
```

WhatsApp → Settings → Linked Devices → Link a Device. The session persists in the
`wa-store` volume across restarts.

Then put nginx in front (`deploy/nginx/whatsapp-mcp.conf`) and connect a client:

```bash
claude mcp add -s user --transport http whatsapp https://your-host/mcp \
  --header "Authorization: Bearer $WHATSAPP_MCP_TOKEN"
```

Full deployment, rollback, and the OS-level safety stack:
[`docs/DEPLOY.md`](docs/DEPLOY.md) and [`deploy/ops/README.md`](deploy/ops/README.md).

### Verify auth is actually on

```bash
# no token MUST be 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://your-host/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

---

## Configuration

Everything is environment variables. The only required one is the token.

| Variable | Default | Purpose |
|---|---|---|
| `WHATSAPP_MCP_TOKEN` | *(required)* | The single owner bearer token |
| `WHATSAPP_MCP_TOKENS_JSON` | unset | Multiple tokens with per-token scopes |
| `WHATSAPP_MCP_TOOLSET` | `minimal` | `minimal` (29 + meta) or `full` (96) |
| `WHATSAPP_MEDIA_ROOTS` | `/tmp/wamcp-media` | Extra dirs a send tool may read from (colon-separated); the built-in media dir is always allowed |
| `WHATSAPP_MCP_AUDIT_LOG` | `/tmp/wamcp-audit.jsonl` | Append-only audit trail (the compose file overrides this to `/var/log/wamcp/audit.jsonl`) |
| `WHATSAPP_MCP_EMBED_WEIGHT` | `0.6` | Semantic weight in hybrid retrieval |
| `VOYAGE_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | unset | Any one enables semantic retrieval |
| `SPEECHMATICS_API_KEYS` / `GROQ_API_KEY` | unset | Enable voice transcription |
| `TRANSCRIBE_LANGUAGE` | `ar` | Transcription language |
| `BRIDGE_MEDIA_TTL_DAYS` | `14` | Downloaded-media reaper, `0` disables |
| `BRIDGE_LOG_MESSAGES` | off | Per-message logging. Off keeps content out of logs |

### Scopes

With `WHATSAPP_MCP_TOKENS_JSON` each token carries scopes, enforced per tool:

`whatsapp:read` · `whatsapp:send` · `whatsapp:admin` · `whatsapp:full`

A read-only token confines a summarising agent to reading and cannot send. The
enforcement applies to `call_tool` dispatch as well as to direct calls, so the
retrieval layer cannot be used to reach a tool the token's scope forbids.

---

## Testing

```bash
pytest                    # 95 offline tests. No WhatsApp, no bridge, no network.
ruff check .
cd whatsapp-bridge && go vet -tags sqlite_fts5 ./...
```

The offline suite covers identity resolution against a synthetic whatsmeow store
and the retrieval invariants (stemming, scoring order, signature rendering,
graceful degradation with no embeddings). It runs in CI on every push.

Three more suites need a live paired session and are run by hand:

| Suite | What it checks |
|---|---|
| `tests/smoke.sh` | Every tool over real HTTP, including the 401 path |
| `tests/toolsearch-eval/` | Retrieval recall@k against the labeled set |
| `tests/voice-harness/` | Transcription quality on real audio |

`sqlite_fts5` is not optional for the bridge. Without the build tag it crash-loops
on the FTS5 virtual table.

---

## Performance

Measured against the upstream project on the same 77k-message store.

- **SQLite indexes** on `messages(chat_jid, timestamp DESC)`,
  `messages(sender, timestamp DESC)`, `messages(timestamp DESC)`,
  `chats(last_message_time DESC)`. Read tools went from a full-table scan (200ms+)
  to index seeks (single-digit ms).
- **FTS5 contentless mirror** on `messages.content` replaces `LIKE '%foo%'`.
  Content search is 12x to 3000x faster depending on selectivity.
- **`include_context=True`** collapsed from `1 + 3N` queries to one query per
  anchor, via a UNION ALL of ordered window slices.
- **History sync batched** into transactions every 500 rows: 10x to 50x faster
  ingest on a fresh pair.
- **Per-thread SQLite connections** with PRAGMAs applied once per thread rather
  than per call (`mmap_size=128MiB`, `query_only`, `temp_store=MEMORY`).
- **WAL bounded** by a 5-minute `wal_checkpoint(TRUNCATE)`, so a long-lived
  read-only reader cannot let the WAL grow without limit.
- **Images**: bridge 201MB to 55MB (distroless, `-trimpath`, `-ldflags="-s -w"`),
  mcp 1.05GB to 509MB (multi-stage uv with a frozen lockfile, static ffmpeg).

---

## Security model

**One credential.** A bearer token in `.env`, verified in the application. No
token or a wrong token is a 401 before any tool runs.

**The bridge is not public.** It is on a private Docker network with no published
port. The mcp server binds loopback only.

**Send paths are sandboxed.** `media_path` is confined to `WHATSAPP_MEDIA_ROOTS`,
so a prompt-injected model cannot ask the bridge to send `messages.db` to a
contact.

**Inputs are clamped** server-side: `limit <= 100`, context windows `<= 20`,
message body `<= 65536` bytes, media `<= 64MB` with a 1MB JSON body cap.

**Mutating calls are audited** to an append-only JSONL log, fsynced per line, and
calls routed through `call_tool` are tagged as such.

**Rate limited** at the bridge (5/s, burst 10) so a runaway agent cannot trip a
WhatsApp temporary ban.

**Least privilege on the host.** `deploy/sudoers.d/mcp-ops` grants exactly the
commands the deploy needs and documents why each omission is an omission:
`systemctl restart nginx` is absent because the project rule is reload-only;
`apt install` is absent because it is too broad.

### What this does not protect you from

Message content goes to your model provider when you use the tools. Self-hosted
storage is not the same as private from the model. The decrypted history lives in
a Docker volume on your box; keep it out of any shared backup.

See [SECURITY.md](SECURITY.md) for the threat model and how to report an issue.

---

## Attribution

Originally forked from [`lharries/whatsapp-mcp`](https://github.com/lharries/whatsapp-mcp)
(MIT), which supplied the whatsmeow bridge skeleton and the first read tools.

Both halves have since been substantially rewritten. Of the 14 files in the
upstream tree, one (`whatsapp-mcp-server/audio.py`) is carried unchanged; the Go
bridge grew about 5x and the Python server about 4x, and everything under
`toolsearch.py`, `transcription.py`, `media.py`, `scheduling.py`,
`observability.py`, `identity.py`, `embeddings.py`, `prompts.py`, `resources.py`,
`deploy/` and `tests/` is new here.

Substantive changes to the inherited code:

1. **whatsmeow updated.** Upstream pinned a version WhatsApp now rejects with
   `405 client outdated`.
2. **`context.Context` threaded** through the five whatsmeow calls that now
   require it, and propagated per request so cancellation reaches uploads.
3. **Fresh-store nil-device fix.** The current `GetFirstDevice` returns
   `(nil, nil)` for an empty store; without a nil check the bridge never shows a
   QR code.
4. **stdio to authenticated Streamable HTTP.** Upstream was stdio with no auth.
5. **Paths and binds are environment-driven** rather than hardcoded.

## License

MIT. See [LICENSE](LICENSE), which carries both copyright lines.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Short version: this repo's one rule is in
[CLAUDE.md](CLAUDE.md), and it is that a rendering is evidence of what a client
chose to draw, and nothing more. Cite the wire, the proto field, or a test you
ran.
