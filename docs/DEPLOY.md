# Deploy runbook

How to build, deploy, verify, and roll back this stack. The box IS production -
`/opt/whatsapp-mcp` is both the git repo and the live deployment; there is no
separate staging. Act carefully and locally.

## TL;DR

```bash
cd /opt/whatsapp-mcp
safebuild mcp            # or: safebuild bridge   (NEVER bare `docker compose build`)
docker compose up -d mcp # or bridge
# verify (see below), then commit + push
```

## The rules that will bite you if ignored

- **Always `safebuild <service>`, never `docker compose build`.** The box is
  ~4GB RAM / 2 vCPU / no GPU and has OOM'd on an unguarded build, taking sshd
  with it. `safebuild` preflights headroom and runs the build on a
  memory-capped buildx builder. If it says "load too high", wait and retry - do
  not `-f` past it unless you've checked `free -m` and `uptime` yourself.
- **The bridge MUST build with `-tags sqlite_fts5`** (set in its Dockerfile) or
  it crash-loops on the FTS5 virtual table.
- **Docker does NOT restart unhealthy containers**, only exited ones. The
  `autoheal` sidecar does that; new containers need the `autoheal: "true"` label.
- **nginx: `sudo nginx -t` then `sudo systemctl reload nginx`, never restart.**
  Never add a 443 listener without its cert already on disk.
- **Secrets live in `/opt/whatsapp-mcp/.env`** (chmod 600, gitignored). Never
  commit it. `.gitignore` covers `.env`, `*.env`, `.env.*`, `*.env.bak*` - a
  timestamped backup once nearly leaked the bearer token, hence the last two.

## Full deploy

1. **Edit code.** Bridge is Go (`whatsapp-bridge/main.go`); MCP is Python
   (`whatsapp-mcp-server/*.py`). Compile-check Python first:
   `python3 -m py_compile whatsapp-mcp-server/<file>.py`.

2. **If you changed the Go bridge's deps** (`go.mod`), nothing special - the
   Dockerfile builds from source. If you changed **Python deps**
   (`pyproject.toml`), regenerate the lock: `cd whatsapp-mcp-server && uv lock`,
   and confirm the pinned `uv` in the Dockerfile can still read it.

3. **Validate compose** if you touched it: `docker compose config --quiet`.

4. **Build** (only the service you changed):
   ```bash
   safebuild mcp        # Python side (tools, transcription, media, server)
   safebuild bridge     # Go side (whatsmeow, storage, endpoints)
   ```

5. **Deploy:**
   ```bash
   docker compose up -d mcp      # recreates just that container
   # bridge recreation re-reads the wa-store volume, so the WhatsApp session
   # persists - it reconnects in ~15s. Watch for it:
   docker logs whatsapp-bridge --tail 5 | grep -i "Connected to WhatsApp"
   ```

6. **Verify (do NOT skip - "deployed" is not "works"):**
   ```bash
   docker compose ps                       # all three healthy
   curl -s -o /dev/null -w '%{http_code}\n' https://whatsapp-mcp.example.com/health   # 200
   # MCP responds + unauth is refused:
   TOKEN=$(grep -oP 'WHATSAPP_MCP_TOKEN=\K.*' .env)
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://whatsapp-mcp.example.com/mcp \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'     # 200
   # fd health (the scheduler-leak canary): should be double digits, not near 65536
   docker exec whatsapp-mcp python -c "import os;print(len(os.listdir('/proc/1/fd')))"
   ```
   For a behavioural change, exercise the actual tool over the MCP (a real
   `tools/call`), not just the health check.

7. **Commit + push** only after verifying. No Claude attribution in commits.

## nginx changes

Edit the repo copy at `deploy/nginx/whatsapp-mcp.conf`, then install and reload:
```bash
sudo tee /etc/nginx/sites-available/whatsapp-mcp < deploy/nginx/whatsapp-mcp.conf >/dev/null
sudo nginx -t && sudo systemctl reload nginx
```

## Debugging with whatsmeow protocol detail

The bridge logs at INFO by default, which hides the protocol exchange. To see
it (e.g. why a message was undecryptable, or the device fan-out of a send):
```bash
BRIDGE_WA_LOG_LEVEL=DEBUG docker compose up -d bridge
# ... reproduce ...
docker compose up -d bridge     # revert to INFO (DEBUG is very chatty)
```

## Rollback

Prior images are tagged (do NOT `docker image prune -a` - it deletes these):
```bash
docker images | grep -E 'whatsapp-(mcp|bridge):rollback'
docker tag whatsapp-mcp:rollback whatsapp-mcp:latest && docker compose up -d mcp
```
`docker image prune -f` (dangling only) is safe; `-a` is not.

## Disk

Today's many rebuilds accumulate. Safe reclamation:
```bash
docker image prune -f                          # dangling images
docker builder prune -f                         # default builder cache
docker buildx prune --builder capped -f         # the safebuild builder's cache
```
Never `prune -a` (kills rollback images) and never delete other projects'
images (other unrelated images).

## Troubleshooting

- **`405 client outdated` in bridge logs.** whatsmeow rot, and the single most
  likely future breakage. `cd whatsapp-bridge && go get go.mau.fi/whatsmeow@latest
  && go get go.mau.fi/util@latest && go mod tidy`, commit, rebuild the bridge,
  re-pair if needed.
- **Tool calls hang or truncate behind a CDN.** A CDN may buffer the streaming
  `/mcp` response. Confirm nginx has `proxy_buffering off` (it does in the
  shipped vhost). The tell is `tools/list` working while calls hang. If it
  persists, add a CDN rule that does not buffer this host, or serve the record
  DNS-only, which loses origin-IP hiding and WAF in exchange.
- **Repeated logouts.** WhatsApp flags datacenter IPs harder than residential
  ones. Re-pair; there is no way around this short of a residential egress.
- **401 on a token you believe is valid.** Check the value reached the container
  (`docker compose exec mcp env | grep WHATSAPP_MCP_TOKEN`) and that the client
  sends `Authorization: Bearer <token>` exactly.
- **QR never appears on a fresh store.** This was a real upstream bug: the
  current `GetFirstDevice` returns `(nil, nil)` rather than an error for an empty
  store. Fixed here; if you see it again after a whatsmeow bump, look there first.

## TLS and CDN

The shipped vhost has placeholder certificate paths. Point them at your own cert
before enabling the 443 block, and never declare a 443 listener whose cert is not
yet on disk: nginx will refuse to load and take every other site on the box with
it. Always `nginx -t` and then `systemctl reload nginx`, never `restart`.

If the host sits behind a proxying CDN, use full/strict TLS to the origin so the
origin certificate is actually validated.
