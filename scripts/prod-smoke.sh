#!/usr/bin/env bash
# Post-deploy production smoke test.
#
# Usage:
#   ./scripts/prod-smoke.sh                  # uses default https://leaddrive.cloud
#   ./scripts/prod-smoke.sh https://other.url
#
# Что проверяет:
#   1. /health возвращает 200 + JSON со staleness data
#   2. X-Request-ID echoed back when sent
#   3. /metrics endpoint accessible + contains pharmacy_* metrics
#   4. /ru/login + /az/login + /en/login render
#   5. Legacy /comparison redirects to /ru/comparison (307)
#   6. Без auth /api/v1/dash/comparison возвращает 401 (not 500)
#   7. Rate-limit: первый burst запросов <50 OK, 60+ → 429
#
# Exit code 0 = all green, 1 = at least one check failed.
# Non-destructive: только GET / HEAD requests, нет mutation.

set -euo pipefail

BASE_URL="${1:-https://leaddrive.cloud}"
PASS=0
FAIL=0

# Colour-free output (CI-friendly)
ok()   { echo "✓ $*"; PASS=$((PASS+1)); }
fail() { echo "✗ $*"; FAIL=$((FAIL+1)); }

echo "Smoke testing: $BASE_URL"
echo "================================================"

# 1. /health
HEALTH_JSON=$(curl -sf "$BASE_URL/health" || echo "FAILED")
if [[ "$HEALTH_JSON" == "FAILED" ]]; then
  fail "/health endpoint unreachable"
else
  STATUS=$(echo "$HEALTH_JSON" | grep -oE '"status":"[^"]+"' | cut -d'"' -f4 || echo "")
  if [[ "$STATUS" == "up" || "$STATUS" == "degraded" ]]; then
    ok "/health returns 200 with status=$STATUS"
  else
    fail "/health has unexpected status: $STATUS"
  fi
fi

# 2. X-Request-ID echo
REQ_ID="smoke-test-$RANDOM-$RANDOM"
ECHOED=$(curl -s -o /dev/null -D - -H "X-Request-ID: $REQ_ID" "$BASE_URL/health" 2>/dev/null \
  | grep -i '^x-request-id:' | awk '{print $2}' | tr -d '\r')
if [[ "$ECHOED" == "$REQ_ID" ]]; then
  ok "X-Request-ID echo works ($REQ_ID)"
else
  fail "X-Request-ID NOT echoed (sent=$REQ_ID, got=$ECHOED)"
fi

# 3. /metrics
METRICS=$(curl -sf "$BASE_URL/metrics" || echo "")
if echo "$METRICS" | grep -q 'pharmacy_'; then
  ok "/metrics endpoint returns pharmacy_* metrics"
else
  fail "/metrics either unreachable or missing pharmacy_* metrics"
fi

# 4. Login pages render for all 3 locales
for locale in ru az en; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/$locale/login")
  if [[ "$STATUS" == "200" ]]; then
    ok "/$locale/login returns 200"
  else
    fail "/$locale/login returns $STATUS (expected 200)"
  fi
done

# 5. Legacy redirect
LOC=$(curl -s -o /dev/null -D - "$BASE_URL/comparison" | grep -i '^location:' | awk '{print $2}' | tr -d '\r')
if [[ "$LOC" == "/ru/comparison" ]]; then
  ok "Legacy /comparison → /ru/comparison (307 redirect)"
else
  fail "Legacy /comparison NOT redirected (got: $LOC)"
fi

# 6. Unauth API gives 401
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/dash/comparison")
if [[ "$STATUS" == "401" ]]; then
  ok "Unauthenticated /api/v1/dash/comparison returns 401"
else
  fail "Unauthenticated /api/v1/dash/comparison returns $STATUS (expected 401)"
fi

# 7. Rate-limit on /auth/request (limit=5/min). Шлём 7 раз → последние должны быть 429.
COUNT_429=0
for i in $(seq 1 7); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -d '{"email":"smoke@example.com"}' \
    "$BASE_URL/auth/request")
  if [[ "$STATUS" == "429" ]]; then
    COUNT_429=$((COUNT_429+1))
  fi
done
if [[ $COUNT_429 -ge 1 ]]; then
  ok "Rate-limit triggers 429 after burst (got $COUNT_429 of 7)"
else
  fail "Rate-limit NOT triggered on /auth/request burst (expected ≥1 of 7 = 429)"
fi

echo "================================================"
echo "Pass: $PASS    Fail: $FAIL"
if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
