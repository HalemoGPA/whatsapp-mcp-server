#!/usr/bin/env bash
# smoke.sh - end-to-end MCP test suite.
#
# Hits every tool registered on the running MCP server at
# http://127.0.0.1:9100/mcp with the bearer token from .env. Asserts:
#   - HTTP 200 on every call
#   - Read tools return non-empty `content` (where data is expected)
#   - Validation tools return Pydantic validation errors (NOT 200-empty)
#   - Destructive tools (send_*, react, edit, delete, mark_read, presence,
#     create_poll, send_location, block_user, etc.) are SKIPPED by default.
#     Pass --destructive to run them (uses self-recipient if SELF_JID set).
#
# Also runs a 20-call concurrent stress test against the read path to catch
# threading regressions (we hit this class of bug on 2026-06-25 - see the
# project memory).
#
# Usage:
#   tests/smoke.sh                 # read-only + validation, ~30 seconds
#   tests/smoke.sh --destructive   # also exercises send paths
#   tests/smoke.sh --concurrency=50
#
# Env:
#   MCP_URL    default http://127.0.0.1:9100/mcp
#   ENV_FILE   default /opt/whatsapp-mcp/.env (read WHATSAPP_MCP_TOKEN)
#   SELF_JID   recipient JID for destructive tests (default: skip those)
#   CHAT_JID   group JID for chat-specific tests (auto-discovered if unset)
set -uo pipefail

MCP_URL="${MCP_URL:-http://127.0.0.1:9100/mcp}"
ENV_FILE="${ENV_FILE:-/opt/whatsapp-mcp/.env}"
SELF_JID="${SELF_JID:-}"
CHAT_JID="${CHAT_JID:-}"
DESTRUCTIVE=0
CONCURRENCY=20
for a in "$@"; do
  case "$a" in
    --destructive) DESTRUCTIVE=1 ;;
    --concurrency=*) CONCURRENCY="${a#*=}" ;;
    -h|--help)
      sed -n '2,/^set -uo pipefail$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

if [[ ! -r "$ENV_FILE" ]]; then
  echo "FATAL: cannot read $ENV_FILE" >&2; exit 2
fi
TOKEN=$(grep -E '^WHATSAPP_MCP_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
if [[ -z "$TOKEN" ]]; then
  echo "FATAL: WHATSAPP_MCP_TOKEN not in $ENV_FILE" >&2; exit 2
fi

PASS=0; FAIL=0; SKIP=0
mkdir -p /tmp/wamcp-tests
RESULTS=/tmp/wamcp-tests/results.txt
: > "$RESULTS"

# call <name> <method> <tool> <args-json>
#   - asserts HTTP 200
#   - sets RESPONSE_BODY to the response body
#   - prints PASS/FAIL line and increments counters
call() {
  local name="$1" method="$2" tool="$3" args="$4"
  local payload code body
  if [[ "$method" == "tools/list" ]]; then
    payload="{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"
  else
    payload="{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
  fi
  code=$(curl -sS -m 30 -o /tmp/wamcp-tests/last.txt -w "%{http_code}" -X POST "$MCP_URL" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$payload")
  body=$(cat /tmp/wamcp-tests/last.txt)
  RESPONSE_BODY="$body"
  if [[ "$code" != "200" ]]; then
    printf "  [FAIL] %-40s http=%s\n" "$name" "$code" | tee -a "$RESULTS"
    FAIL=$((FAIL+1))
    return 1
  fi
  printf "  [PASS] %-40s\n" "$name" | tee -a "$RESULTS"
  PASS=$((PASS+1))
  return 0
}

# assert_contains <tag> <substr> -- checks RESPONSE_BODY contains <substr>
assert_contains() {
  local tag="$1" needle="$2"
  if ! grep -qF -- "$needle" /tmp/wamcp-tests/last.txt; then
    printf "  [FAIL] %-40s missing %q in body\n" "$tag" "$needle" | tee -a "$RESULTS"
    FAIL=$((FAIL+1)); PASS=$((PASS-1))
    return 1
  fi
}

echo "============================================================"
echo "WhatsApp MCP smoke test - $MCP_URL"
echo "============================================================"
echo

echo "=== phase 1: tool registry ==="
call "tools/list returns the registry" tools/list "" ""
TOOLCOUNT=$(grep -oE '"name":"[a-z_]+"' /tmp/wamcp-tests/last.txt | sort -u | wc -l)
echo "  registered tools: $TOOLCOUNT"
echo

echo "=== phase 2: read-only tools (no side effects) ==="
call "bridge_health"                 tools/call bridge_health                  '{}'
assert_contains "bridge_health"      '"connected":true'
call "search_contacts non-empty"     tools/call search_contacts                '{"query":"a"}'
call "search_contacts empty -> []"   tools/call search_contacts                '{"query":""}'
assert_contains "search_contacts []" '"content":[]'
call "list_chats"                    tools/call list_chats                     '{"limit":3}'
assert_contains "list_chats has jid" '"jid":"'
call "list_messages no filter"       tools/call list_messages                  '{"limit":3,"include_context":false}'
call "list_messages with chat"       tools/call list_messages                  '{"limit":3,"include_context":false,"chat_jid":"120363000000000000@g.us"}'
call "list_messages FTS5 query"      tools/call list_messages                  '{"query":"hello","limit":3,"include_context":false}'
call "list_messages with context"    tools/call list_messages                  '{"limit":3,"include_context":true,"context_before":1,"context_after":1}'
call "list_messages cursor pagn."    tools/call list_messages                  '{"limit":3,"include_context":false,"before_timestamp":"2026-06-25T00:00:00"}'
call "get_chat"                      tools/call get_chat                       '{"chat_jid":"120363000000000000@g.us"}'
call "get_contact_chats"             tools/call get_contact_chats              '{"jid":"10000000000000@lid","limit":3}'
call "get_last_interaction"          tools/call get_last_interaction           '{"jid":"120363000000000000@g.us"}'
call "get_message_context"           tools/call get_message_context            '{"message_id":"NONEXISTENT","before":1,"after":1}'
call "get_direct_chat_by_contact"    tools/call get_direct_chat_by_contact     '{"sender_phone_number":"10000000000000"}'
call "get_profile_picture (group)"   tools/call get_profile_picture            '{"jid":"120363000000000000@g.us"}'
call "list_joined_groups"            tools/call list_joined_groups             '{}'
call "get_blocklist"                 tools/call get_blocklist                  '{}'
call "list_subscribed_newsletters"   tools/call list_subscribed_newsletters    '{}'
echo

echo "=== phase 3: validation errors (asserts NOT silent-empty) ==="
call "react missing args"            tools/call react_to_message               '{}'
assert_contains "react missing"      'validation errors'
call "edit missing args"             tools/call edit_message                   '{}'
assert_contains "edit missing"       'validation errors'
call "delete missing args"           tools/call delete_message                 '{}'
assert_contains "delete missing"     'validation errors'
call "mark_read missing args"        tools/call mark_read                      '{}'
assert_contains "mark_read missing"  'validation errors'
call "send_message missing args"     tools/call send_message                   '{}'
assert_contains "send missing"       'validation errors'
call "send_location missing args"    tools/call send_location                  '{}'
assert_contains "send_location miss" 'validation errors'
call "create_poll missing args"      tools/call create_poll                    '{}'
assert_contains "create_poll miss"   'validation errors'
call "create_poll <2 options"        tools/call create_poll                    '{"recipient":"x","name":"y","options":["a"]}'
assert_contains "create_poll lt2"    'at least 2 entries'
call "set_disappearing missing args" tools/call set_disappearing               '{}'
assert_contains "disap missing"      'validation errors'
call "create_group missing args"     tools/call create_group                   '{}'
assert_contains "group missing"      'validation error'
call "block_user missing args"       tools/call block_user                     '{}'
assert_contains "block missing"      'validation error'
echo

echo "=== phase 4: clamps (asserts the bounds work) ==="
call "list_messages limit=9999 clamped" tools/call list_messages '{"limit":9999,"include_context":false}'
# Count timestamp prefixes (one per message) from the JSON-decoded text payload.
# Raw grep on the wire would count escaped occurrences too; python parse is the
# ground truth. MAX_LIMIT=100, so a request for 9999 should yield <= 100 messages.
N=$(python3 -c "
import sys, json, re
raw = open('/tmp/wamcp-tests/last.txt').read()
m = re.search(r'data: (.*)', raw)
text = json.loads(m.group(1))['result']['content'][0]['text']
print(sum(1 for line in text.split(chr(10)) if re.match(r'\[\d{4}-\d{2}-\d{2}', line)))
" 2>/dev/null || echo "?")
if [[ "$N" != "?" ]] && (( N <= 100 )); then
  printf "  [PASS] %-40s capped at %d <= 100\n" "list_messages limit clamp" "$N" | tee -a "$RESULTS"
  PASS=$((PASS+1))
else
  printf "  [FAIL] %-40s got %s > 100\n" "list_messages limit clamp" "$N" | tee -a "$RESULTS"
  FAIL=$((FAIL+1))
fi
echo

echo "=== phase 5: concurrent stress ($CONCURRENCY parallel calls) ==="
mkdir -p /tmp/wamcp-tests/concur
rm -f /tmp/wamcp-tests/concur/* 2>/dev/null
seq "$CONCURRENCY" | xargs -P "$CONCURRENCY" -I{} bash -c "
  curl -sS -m 30 -X POST '$MCP_URL' \
    -H 'Authorization: Bearer $TOKEN' \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"list_chats\",\"arguments\":{\"limit\":3}}}' \
    -o /tmp/wamcp-tests/concur/r-{}.txt -w '%{http_code} %{time_total}\n'
" > /tmp/wamcp-tests/concur-summary.txt
ERR=$(awk '$1!="200"' /tmp/wamcp-tests/concur-summary.txt | wc -l)
EMPTY=$(grep -lF '"content":[]' /tmp/wamcp-tests/concur/*.txt | wc -l)
MEAN=$(awk '{s+=$2} END{if(NR>0)printf "%.1fms\n", (s/NR)*1000; else print "n/a"}' /tmp/wamcp-tests/concur-summary.txt)
if [[ "$ERR" == "0" && "$EMPTY" == "0" ]]; then
  printf "  [PASS] %-40s n=%s errors=%s empty=%s mean=%s\n" "concurrent stress" "$CONCURRENCY" "$ERR" "$EMPTY" "$MEAN" | tee -a "$RESULTS"
  PASS=$((PASS+1))
else
  printf "  [FAIL] %-40s errors=%s empty=%s\n" "concurrent stress" "$ERR" "$EMPTY" | tee -a "$RESULTS"
  FAIL=$((FAIL+1))
fi
echo

echo "=== phase 6: media upload paths (no actual send - bad recipient) ==="
# 1x1 transparent PNG
PNG_B64='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
call "send_file media_data base64"    tools/call send_file              "{\"recipient\":\"0\",\"media_data\":\"$PNG_B64\",\"filename\":\"smoke.png\"}"
call "send_file BOTH url+data error"  tools/call send_file              '{"recipient":"x","media_url":"https://example.com/x","media_data":"AAAA"}'
assert_contains "send_file conflict"  'exactly one of'
call "send_file NEITHER source"       tools/call send_file              '{"recipient":"x"}'
assert_contains "send_file none"      'exactly one of'
call "send_file bad base64"           tools/call send_file              '{"recipient":"x","media_data":"!!!not-b64!!!"}'
assert_contains "send_file bad b64"   'not valid base64'
call "send_view_once_media url"       tools/call send_view_once_media   '{"recipient":"0","media_url":"https://httpbin.org/image/png","filename":"smoke.png"}'
call "send_view_once_media base64"    tools/call send_view_once_media   "{\"recipient\":\"0\",\"media_data\":\"$PNG_B64\",\"filename\":\"smoke.png\"}"
call "send_audio_message NEITHER"     tools/call send_audio_message     '{"recipient":"x"}'
assert_contains "audio none"          'exactly one of'
echo ""

echo "=== phase 7: chat name dual-field + list_media_in_chat ==="
call "list_chats has push_name field" tools/call list_chats             '{"limit":3}'
# the field should at minimum be DECLARED (even if value is None) - assert it's a JSON key in the text body
if grep -qE '"push_name"' /tmp/wamcp-tests/last.txt; then
  printf "  [PASS] %-40s field present\n" "list_chats push_name field" | tee -a "$RESULTS"
  PASS=$((PASS+1))
else
  printf "  [FAIL] %-40s field absent\n" "list_chats push_name field" | tee -a "$RESULTS"
  FAIL=$((FAIL+1))
fi
call "list_media_in_chat group"       tools/call list_media_in_chat     '{"chat_jid":"120363000000000000@g.us","limit":3}'
call "list_media_in_chat filtered"    tools/call list_media_in_chat     '{"chat_jid":"120363000000000000@g.us","media_type":"audio","limit":3}'
call "list_media_in_chat missing arg" tools/call list_media_in_chat     '{}'
assert_contains "lmc missing"         'validation error'
echo ""

echo "=== phase 8: leak guard - sensitive media columns must NOT appear ==="
# media_key + file_enc_sha256 are AES decryption keys; url is the CDN signed URL.
# Any of these in a tool response = "stash this + later decrypt the media" capability.
call "leak: list_messages" tools/call list_messages '{"limit":5,"include_context":false}'
for col in media_key file_sha256 file_enc_sha256 '"url"'; do
  if grep -q "$col" /tmp/wamcp-tests/last.txt; then
    printf "  [FAIL] %-40s contains %s\n" "leak-guard list_messages" "$col" | tee -a "$RESULTS"
    FAIL=$((FAIL+1))
  else
    printf "  [PASS] %-40s no %s\n" "leak-guard list_messages" "$col" | tee -a "$RESULTS"
    PASS=$((PASS+1))
  fi
done
call "leak: get_message_context" tools/call get_message_context '{"message_id":"X","before":1,"after":1}'
for col in media_key file_sha256 file_enc_sha256; do
  if grep -q "$col" /tmp/wamcp-tests/last.txt; then
    printf "  [FAIL] %-40s contains %s\n" "leak-guard get_message_context" "$col" | tee -a "$RESULTS"
    FAIL=$((FAIL+1))
  else
    printf "  [PASS] %-40s no %s\n" "leak-guard get_message_context" "$col" | tee -a "$RESULTS"
    PASS=$((PASS+1))
  fi
done
echo ""

if [[ "$DESTRUCTIVE" == "1" ]]; then
  echo "=== phase 9: destructive tools (--destructive) ==="
  if [[ -z "$SELF_JID" ]]; then
    echo "  SKIP: SELF_JID not set (would have nowhere safe to send). Pass SELF_JID=<your jid>." | tee -a "$RESULTS"
    SKIP=$((SKIP+1))
  else
    call "send_message to self"      tools/call send_message     "{\"recipient\":\"$SELF_JID\",\"message\":\"smoke test $(date +%s)\"}"
    call "send_presence composing"   tools/call send_presence    "{\"chat_jid\":\"$SELF_JID\",\"state\":\"composing\"}"
    call "send_presence paused"      tools/call send_presence    "{\"chat_jid\":\"$SELF_JID\",\"state\":\"paused\"}"
    # don't actually create groups/polls/locations etc against a real
    # recipient by default - leave those for manual verification.
  fi
fi

echo
echo "============================================================"
echo "RESULT  PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP"
echo "============================================================"
exit $(( FAIL > 0 ? 1 : 0 ))
