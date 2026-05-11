# Load Tests

k6-based load tests for Pharmacy Monitor API.

## Why k6 and not locust?

- Single binary, no Python deps
- Native JS scripting (matches frontend's TS code style)
- Built-in thresholds → CI fails on regression
- HTML report + Grafana integration available out-of-box

## Install

```bash
# macOS
brew install k6

# Linux (Hetzner)
sudo apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb https://dl.k6.io/deb stable main" | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt update
sudo apt install k6
```

## Run

```bash
# Get a JWT cookie from the API (magic-link flow):
TOKEN=$(.venv/bin/python -c "
from src.storage import init_db, make_session
from src.tenants import issue_magic_token, get_or_create_default, add_user
init_db()
S = make_session(); s = S()
t = get_or_create_default(s)
add_user(s, t.id, 'loadtest@example.com', name='LoadTest', role='admin')
s.commit()
print(issue_magic_token(s, 'loadtest@example.com'))
")
curl -c cookies.txt "http://localhost:8080/auth/verify?token=$TOKEN" > /dev/null
JWT=$(awk '$6 == "pm_session" {print $7}' cookies.txt)

# Run load test
BASE_URL=http://localhost:8080 \
PHARMACY_JWT="$JWT" \
PHARMACY_API_KEY=test-key \
k6 run load-test/api.k6.js
```

## Acceptance criteria (CI thresholds)

| Metric | Threshold |
|---|---|
| p95 latency (all endpoints) | < 500ms |
| p95 latency (dashboard endpoints) | < 1000ms |
| Error rate (4xx + 5xx, excluding 401) | < 1% |
| Successful checks | > 99% |

If thresholds violated → exit code 99 → CI step fails.

## Capacity model

Test runs 100 concurrent VUs against the API. With this stage profile:
- Total: ~3300 requests over 4 minutes
- Each VU: 1 dashboard cycle (10 endpoints) + 1 ERP cycle (4 endpoints) per ~1.5s
- Realistic for 50-100 active users at any moment

If we need to scale beyond 100 users:
1. Add Redis caching for `/dash/*` endpoints (5-10s TTL)
2. Postgres connection pool tuning (`pool_size=20`, `max_overflow=10`)
3. Add uvicorn workers (currently 2 in systemd, can go to vCPU count)
4. Consider Hetzner CX32 (8GB → 16GB RAM) if DB hot under load

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| All requests 401 | JWT cookie expired or wrong domain |
| `connect: connection refused` | API not running on $BASE_URL |
| p95 climbs over time (memory leak) | DB connection not released → check `request.state.db` lifecycle |
| Sudden 503s after N seconds | Hit rate limiter — increase `PHARMACY_API_RATE_LIMIT_RPM` for test or add user-pool |
