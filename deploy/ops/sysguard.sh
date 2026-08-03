#!/usr/bin/env bash
# sysguard - lightweight metric logger + threshold tripwire.
# Runs every minute via cron; writes one JSON line per minute to LOG.
# When memory drops below ALERT_AVAIL_MB it ALSO writes to ALERT_LOG so we
# have a fast-find forensic trail when something explodes.
#
# Install:
#   sudo install -m 0755 sysguard.sh /usr/local/bin/sysguard
#   ( sudo crontab -l 2>/dev/null | grep -v sysguard
#     echo "* * * * * /usr/local/bin/sysguard" ) | sudo crontab -
#
# Inspect:
#   sudo tail -f /var/log/sysguard.log /var/log/sysguard-alert.log
set -euo pipefail

LOG="${LOG:-/var/log/sysguard.log}"
ALERT_LOG="${ALERT_LOG:-/var/log/sysguard-alert.log}"
ALERT_AVAIL_MB="${ALERT_AVAIL_MB:-400}"
ALERT_DISK_GB="${ALERT_DISK_GB:-5}"
ALERT_LOAD1M="${ALERT_LOAD1M:-3.0}"

# Optional webhook config. If WEBHOOK_URL is set in /etc/sysguard.env, alerts
# also POST to it as JSON. Cooldown prevents flooding when an alert persists.
WEBHOOK_ENV="${WEBHOOK_ENV:-/etc/sysguard.env}"
WEBHOOK_COOLDOWN_S="${WEBHOOK_COOLDOWN_S:-600}"  # 10 min between repeat alerts
WEBHOOK_STATE="${WEBHOOK_STATE:-/var/lib/sysguard-last-alert}"
[[ -f "$WEBHOOK_ENV" ]] && . "$WEBHOOK_ENV"

ts=$(date -Iseconds)
read mem_total mem_used mem_free mem_avail < <(
  free -m | awk '/^Mem:/{print $2, $3, $4, $7}'
)
read swap_total swap_used < <(free -m | awk '/^Swap:/{print $2, $3}')
disk_gb=$(df --output=avail -BG / | awk 'NR==2{sub("G","",$1); print $1}')
load1=$(awk '{print $1}' /proc/loadavg)
load5=$(awk '{print $2}' /proc/loadavg)
top_rss=$(ps -eo rss,comm --sort=-rss --no-headers | head -1)
top_rss_mb=$(awk '{printf "%d", $1/1024}' <<<"$top_rss")
top_rss_cmd=$(awk '{$1=""; print $0}' <<<"$top_rss" | sed 's/^ //')

line=$(printf '{"ts":"%s","mem":{"total":%s,"used":%s,"free":%s,"avail":%s},"swap":{"total":%s,"used":%s},"disk_gb_free":%s,"load":{"1m":%s,"5m":%s},"top":{"rss_mb":%s,"cmd":"%s"}}' \
  "$ts" "$mem_total" "$mem_used" "$mem_free" "$mem_avail" \
  "$swap_total" "$swap_used" "$disk_gb" "$load1" "$load5" \
  "$top_rss_mb" "$top_rss_cmd")
echo "$line" >> "$LOG"

alert=""
if [[ "$mem_avail" -lt "$ALERT_AVAIL_MB" ]]; then
  alert="LOW_MEM avail=${mem_avail}MB (threshold ${ALERT_AVAIL_MB}MB)"
fi
if [[ "$disk_gb" -lt "$ALERT_DISK_GB" ]]; then
  alert="${alert:+$alert; }LOW_DISK free=${disk_gb}GB (threshold ${ALERT_DISK_GB}GB)"
fi
if awk -v l="$load1" -v m="$ALERT_LOAD1M" 'BEGIN{ exit !(l>m) }'; then
  alert="${alert:+$alert; }HIGH_LOAD 1m=${load1} (threshold ${ALERT_LOAD1M})"
fi
if [[ -n "$alert" ]]; then
  printf '%s ALERT %s | top=%sMB %s\n' "$ts" "$alert" "$top_rss_mb" "$top_rss_cmd" >> "$ALERT_LOG"

  # Webhook (optional). Honours cooldown so a stuck-low-memory state doesn't
  # produce a flood. Curl uses --max-time so a slow webhook never blocks cron.
  if [[ -n "${WEBHOOK_URL:-}" ]] && command -v curl >/dev/null; then
    now_epoch=$(date +%s)
    last_epoch=0
    [[ -f "$WEBHOOK_STATE" ]] && last_epoch=$(cat "$WEBHOOK_STATE" 2>/dev/null || echo 0)
    if (( now_epoch - last_epoch >= WEBHOOK_COOLDOWN_S )); then
      payload=$(printf '{"ts":"%s","host":"%s","alert":"%s","mem_avail_mb":%s,"swap_used_mb":%s,"disk_free_gb":%s,"load_1m":%s,"top_rss_mb":%s,"top_cmd":"%s"}' \
        "$ts" "$(hostname)" "$alert" "$mem_avail" "$swap_used" "$disk_gb" "$load1" "$top_rss_mb" "$top_rss_cmd")
      curl -sS --max-time 5 -H 'Content-Type: application/json' \
        -X POST -d "$payload" "$WEBHOOK_URL" >/dev/null 2>>"$ALERT_LOG" || true
      echo "$now_epoch" > "$WEBHOOK_STATE"
    fi
  fi
fi

for f in "$LOG" "$ALERT_LOG"; do
  [[ -f "$f" ]] || continue
  size=$(stat -c%s "$f" 2>/dev/null || echo 0)
  if [[ "$size" -gt 5242880 ]]; then
    mv -f "$f" "$f.1"
  fi
done
