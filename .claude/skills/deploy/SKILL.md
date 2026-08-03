---
name: deploy
description: Build, deploy, verify, or roll back this server on the host. Use when asked to ship a change, rebuild a container, update nginx, or diagnose why the service is down. Covers the memory-capped build, the reload-not-restart rule, and the unhealthy-container failure mode.
---

# Deploying

This runs on a small VPS shared with other services. Typical free RAM is around
1.5GB. An unguarded `docker compose build` once OOM'd the box and took sshd down
with it. Two rules follow, and both have already been learned the hard way.

## Before anything heavy

```bash
free -m
cat /proc/loadavg
```

## Build

```bash
./deploy/safebuild <service>       # mcp | bridge
```

`safebuild` preflights headroom and runs the build inside a memory-capped
`systemd-run` cgroup. **Never `docker compose build` directly.** An earlier
version of the wrapper capped the CLI rather than BuildKit, so the cap was a
no-op; if you touch it, verify the cap applies to the builder, not the client.

The bridge must build with `-tags sqlite_fts5` or it crash-loops on the FTS5
virtual table.

## Deploy

```bash
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:9100/health          # {"ok": true}
```

## nginx

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**Reload, never restart.** Other sites share this nginx. If `nginx -t` fails,
remove the symlink, reload, and stop. Do not iterate on a broken config with the
service down.

Never declare a 443 listener whose certificate is not already on disk. nginx will
refuse to load and every site on the box goes with it.

## Verify

```bash
# unauthenticated must be 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://your-host/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

bash tests/smoke.sh
```

Then confirm you did not push the box over:

```bash
docker stats --no-stream     # any container near its mem_limit?
free -m                      # available dropping, swap climbing?
```

## When it says "Up" but nothing works

**Docker does not restart unhealthy containers, only exited ones.** A container
can sit `Up` and broken indefinitely; one outage ran 18 hours while `docker ps`
looked fine. The `autoheal` sidecar handles this, and every container must carry
`autoheal: "true"` for it to apply.

If a service is wedged, check the healthcheck before the logs:

```bash
docker inspect --format '{{.State.Health.Status}}' <container>
```

## Rollback

```bash
git revert <sha> && ./deploy/safebuild <service> && docker compose up -d
```

Roll back rather than fixing forward when the box is degraded. A second build
under memory pressure is how the first outage got worse.

## Traps worth remembering

- `with sqlite3.connect(...) as c` scopes a **transaction**, not the connection.
  It does not close. Use the `_conn()` context manager in `scheduling.py`. A leak
  here exhausted the file-descriptor limit and took the server down silently.
- Normalise `@lid` to a phone-number JID on anything that came from an event.
  Messages from the owner's own phone arrive LID-addressed, and forgetting this
  breaks joins with no error, just wrong counts.
- Any concurrency-relevant change needs 10+ parallel calls to test. A sequential
  probe reuses one pool thread and misses thread-bound bugs entirely.
