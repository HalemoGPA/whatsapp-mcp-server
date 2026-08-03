# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/HalemoGPA/whatsapp-mcp-server/security/advisories/new)
rather than a public issue. Please include what you did, what happened, and what
you expected. There is no bounty; there is a fast reply and credit in the fix.

## What this software is

A single-owner MCP server that reads and writes **your own** WhatsApp account
through a linked-device session. It is not multi-tenant. It is not a WhatsApp
Business API client. Deploying it means putting a decrypted copy of your message
history on a machine you administer.

## Threat model

### Defended

| Threat | Control |
|---|---|
| Anyone reaching `/mcp` without the token | Bearer verification in the application, not just at the proxy. No token or a wrong token is a 401 before any tool runs. |
| Direct access to the WhatsApp bridge | The bridge container is `expose:`-only, on a private Docker network, with no published port. |
| Proxy misconfiguration opening the server | Auth is in the app, so a broken nginx config cannot bypass it. |
| A prompt-injected model exfiltrating the database | Send paths confine `media_path` to `WHATSAPP_MEDIA_ROOTS`. The model cannot ask the bridge to send `messages.db` to a contact. |
| An over-broad agent doing irreversible damage | Destructive tools require `confirm=True` as an explicit argument, and are annotated `destructiveHint` so clients can prompt. |
| A retrieval layer bypassing authorization | `call_tool` dispatches in-process, past the middleware chain, so `toolsearch.py` re-applies scope checks and audit logging. This is deliberate and tested; treat any change there as security-relevant. |
| Resource exhaustion | `limit <= 100`, context windows `<= 20`, message body `<= 65536` bytes, media `<= 64MB`, JSON bodies `<= 1MB`, and a 5/s (burst 10) send limiter. |
| A runaway agent triggering a WhatsApp ban | The same send rate limiter, at the bridge rather than in the tool layer, so every path is covered. |
| Silent tampering | Every mutating call is appended to a JSONL audit log, fsynced per line, tagged when routed via `call_tool`. |
| Credentials in git | `.env`, `.env.*`, and timestamped backups are gitignored. CI runs a secret scan on every push. |
| Over-broad host privileges | `deploy/sudoers.d/mcp-ops` grants named commands only, and documents why each omission is an omission. |

### Not defended

**Your model provider sees message content.** When you use these tools, the
messages they return go to whatever model is calling them. Self-hosted storage is
not the same thing as private from the model. This is inherent to any cloud-model
MCP server, and it is the single most important line on this page.

**The decrypted history is on your disk.** It lives in the `wa-store` Docker
volume. Anyone with root on that box, or a copy of that volume, has your entire
message history. Keep the box patched and keep the volume out of any shared or
off-site backup you do not control.

**A stolen token is full access.** There is one credential and it does not
expire. Rotate it by editing `.env` and running `docker compose up -d`, then
update the client header. Use `WHATSAPP_MCP_TOKENS_JSON` with narrow scopes if
more than one agent needs access; a read-only token cannot send.

**Other people in your chats did not consent.** Your message history contains
theirs. That is a fact about the data, not a bug in the software, and no control
here changes it.

**The linked-device session can be revoked at any time.** WhatsApp flags
datacenter IPs more aggressively than residential ones. This is a design
constraint, not an attack.

## Scope

In scope: authentication bypass, scope-enforcement bypass (especially through
`call_tool`), path traversal in the send sandbox, injection in the query layer,
audit-log evasion, and anything that lets an unauthenticated caller reach the
bridge.

Out of scope: the fact that a valid token grants full access by design, WhatsApp
protocol behaviour we merely observe, and findings that require root on the host.

## Supported versions

`main` only. This is a single-deployment project, not a distributed release.
