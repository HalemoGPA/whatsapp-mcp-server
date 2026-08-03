# Operational safety stack

Defense-in-depth against OOM and runaway builds for the WhatsApp MCP bridge
+ mcp containers on a shared, RAM-constrained VPS (~4GB, co-hosting other
services).

Born from an early outage where `docker compose build --no-cache mcp`
without swap drove free RAM past the kernel's OOM-kill threshold and took
out sshd worker processes. With this stack installed, that same build would
hit a hard 1.5GB cgroup ceiling and the box's other services would stay up.

## What's here

- **`../safebuild`** (`deploy/safebuild`) — `safebuild <service>` runs
  `docker compose build` under a **real** cgroup memory cap, after refusing to
  start if `free -m available < 1000MB`, load > 1.5, or `df / < 5GB`. It runs
  the build on a `docker-container` buildx builder, whose buildkitd is itself a
  container, so `--driver-opt memory` becomes a real `memory.max` on the process
  that does the work. An earlier version wrapped the build in `systemd-run
  -p MemoryMax=...`; that never bound anything, because BuildKit builds inside
  dockerd (`/system.slice/docker.service`, `MemoryMax=infinity`), not the
  caller's cgroup - the header of `deploy/safebuild` documents the rewrite. Use
  `-f` to override the preflight gate. Env-tunable: `MIN_AVAIL_MB`,
  `MIN_DISK_GB`, `MAX_LOAD_1M`, `MEM_CAP_MB`, `COMPOSE_DIR`. Needs no `sudo`.

- **`sysguard.sh`** — cron-driven (every minute) JSON metric logger.
  Writes one JSON line per minute to `/var/log/sysguard.log`
  (mem/swap/disk/load/top-RSS), and an alert to
  `/var/log/sysguard-alert.log` when `available_mem < 400MB`,
  `disk_free < 5GB`, or `load_1m > 3.0`. Optional webhook POST on alert
  via `WEBHOOK_URL` in `/etc/sysguard.env` (10-min cooldown so a stuck
  state doesn't flood). Both logs rotate at 5MB.

## Install (once per host)

```bash
# Install the wrapper + watchdog binaries
sudo install -m 0755 deploy/safebuild        /usr/local/bin/safebuild
sudo install -m 0755 deploy/ops/sysguard.sh  /usr/local/bin/sysguard

# Cron entry
( sudo crontab -l 2>/dev/null | grep -v '/usr/local/bin/sysguard'
  echo "* * * * * /usr/local/bin/sysguard" ) | sudo crontab -

# Log files (root-owned, adm-group readable)
sudo touch /var/log/sysguard.log /var/log/sysguard-alert.log
sudo chown root:adm /var/log/sysguard*.log
sudo chmod 0640 /var/log/sysguard*.log
```

## Pair with the OS-level layer

The full layered stack also includes:

- **Swap** (2GB swapfile + `vm.swappiness=10`) — kernel can page out cold
  memory instead of insta-killing. Adds minutes of warning, not zero.
- **`earlyoom`** daemon (`apt install earlyoom`) — fires SIGTERM at 8% mem
  + 5% swap free, *before* the kernel OOM killer. `--avoid` list protects
  `sshd / dockerd / nginx / whatsapp-{bridge,mcp}`; `--prefer` targets
  build hogs (`go / cc1 / ld / python / uv / pip / node / next`).
- **sshd / dockerd OOMScoreAdjust=-1000 / -500** via systemd drop-ins in
  `/etc/systemd/system/{ssh,docker}.service.d/override.conf`. Guarantees
  ssh stays reachable under any pressure - no more "had to reboot, couldn't
  ssh in".
- **`vm.overcommit_ratio=80 vm.panic_on_oom=0 vm.vfs_cache_pressure=50`**
  in `/etc/sysctl.d/90-mem-safety.conf`.

## Usage

```bash
# rebuild after editing the MCP server, with hard memory ceiling
safebuild mcp

# rebuild after editing the bridge
safebuild bridge

# force past the preflight gate (still capped by the cgroup)
safebuild -f mcp

# tail the sysguard timeline
sudo tail -f /var/log/sysguard.log

# fast-find recent alerts
sudo tail -f /var/log/sysguard-alert.log

# wire a webhook so alerts go somewhere (Slack/Discord/etc.)
echo 'WEBHOOK_URL=https://hooks.slack.com/services/...' \
  | sudo tee /etc/sysguard.env
sudo chmod 0600 /etc/sysguard.env
```

---

## Deferred infra (documented, not yet deployed)

### N5 disaster recovery

Two lines of defense:

1. **Litestream sidecar** streams `messages.db` and `whatsapp.db` (the
   whatsmeow session store) to any S3-compatible object storage (e.g. Backblaze B2).
   Add to `docker-compose.yml`:

       litestream:
         image: litestream/litestream:0.3.13
         restart: unless-stopped
         command: replicate
         volumes:
           - wa-store:/data:ro
           - ./deploy/litestream.yml:/etc/litestream.yml:ro
         environment:
           LITESTREAM_ACCESS_KEY_ID: ${B2_KEY_ID}
           LITESTREAM_SECRET_ACCESS_KEY: ${B2_SECRET}

2. **Cron tarball** as belt-and-braces:

       0 4 * * *  tar czf /var/backups/wa-store-$(date +%F).tar.gz \
                    -C /var/lib/docker/volumes/whatsapp-mcp_wa-store _data \
                  && find /var/backups -name 'wa-store-*.tar.gz' -mtime +7 -delete

Restore RTO target ~15 min: stop the stack, `litestream restore` into a
fresh volume, `docker compose up -d`. Session keys survive; no re-pairing.

### N6 enrichment (transcribe + OCR)

Auto-transcribe voice notes + OCR screenshots.

- Bridge posts `{message_id, chat_jid, media_type}` to `ENRICH_WEBHOOK_URL`
  on each new media message.
- Sidecar hosts `faster-whisper tiny.en` for `audio/ptt`, Tesseract for images.
- Results land in a new `message_enrichments` table on the bridge,
  joined into `list_messages` output.
- Per-chat `enrichment_policy` (`always` / `never` / `on_request`).

Cost: `faster-whisper` RSSes ~250 MB even tiny.en. On a 4 GB box,
run the enrichment sidecar on a separate host and pipe results back over
HTTPS + HMAC.

### N8 Cloudflare Access

Fronts `whatsapp-mcp.example.com`:

1. Zero Trust dashboard: create Access Application for the hostname.
2. Policy `Allow`, include=`owner@example.com` (or a service token).
3. nginx verifies CF JWT before proxying:

       location = /_cf_auth {
           internal;
           proxy_pass https://example.cloudflareaccess.com/cdn-cgi/access/authorized;
           proxy_pass_request_body off;
           proxy_set_header X-Original-URI $request_uri;
       }
       location /mcp/ {
           auth_request /_cf_auth;
           proxy_pass http://127.0.0.1:9100/mcp/;
           # existing headers ...
       }

Or use `cloudflared tunnel` and yank the public DNS A record entirely.

---

## Realtime + auth quickrefs (already deployed)

### N3 SSE stream + webhooks

- `GET http://bridge:8080/api/events/stream` (loopback / docker-net only)
  returns `text/event-stream`. Each event is a JSON `bridgeEvent`
  (`type`, `chat_jid`, `message_id`, `sender`, `is_from_me`, `content`,
  `media_type`, `timestamp`). 15 s ping heartbeat.
- `WHATSAPP_WEBHOOK_URL` in `.env` enables outbound POSTs. Add
  `WHATSAPP_WEBHOOK_SECRET` to sign with `X-Wamcp-Signature: sha256=<hex>`.

### G11 + N7 auth

Legacy single token still works:

    WHATSAPP_MCP_TOKEN=some-long-random-string

Multi-token registry with per-tool scopes:

    WHATSAPP_MCP_TOKENS_JSON='[
      {"token":"aaa...","client_id":"phone","scopes":["whatsapp:read","whatsapp:send"]},
      {"token":"bbb...","client_id":"bi",   "scopes":["whatsapp:read"]}
    ]'

Scopes: `whatsapp:read`, `whatsapp:send`, `whatsapp:admin` or
`whatsapp:full`. Per-tool scope map in `observability.py` (`TOOL_SCOPES`).
