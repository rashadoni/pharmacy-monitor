# E2E Tests

Playwright tests against real backend.

## Setup

```bash
cd frontend
pnpm install
pnpm exec playwright install chromium webkit
```

## Run locally

Terminal 1 — backend:
```bash
cd ..
DATABASE_URL=sqlite:///data/db.sqlite \
PHARMACY_API_KEY=test-key \
JWT_SECRET=test-secret-very-long \
PHARMACY_AUTH_DEV_SHOW_TOKEN=1 \
.venv/bin/uvicorn src.api:app --port 8080
```

Terminal 2 — frontend:
```bash
cd frontend
NEXT_PUBLIC_API_URL=http://localhost:8080 pnpm dev
```

Terminal 3 — generate auth token + run tests:
```bash
# Issue magic-token for test user
TOKEN=$(cd .. && .venv/bin/python -c "
from src.storage import init_db, make_session
from src.tenants import get_or_create_default, add_user, issue_magic_token
init_db()
S = make_session(); s = S()
t = get_or_create_default(s)
u = add_user(s, t.id, 'e2e@example.com', name='E2E', role='admin')
s.commit()
print(issue_magic_token(s, 'e2e@example.com'))
")

cd frontend
PLAYWRIGHT_AUTH_TOKEN=$TOKEN pnpm test:e2e
```

## In CI (GitHub Actions)

The `frontend` job in `.github/workflows/ci.yml` runs `pnpm test` (vitest unit tests).
E2E is run separately when you want it via:

```bash
gh workflow run e2e
```

(Workflow file `.github/workflows/e2e.yml` to be added when prod is up.)

## Coverage

| Area | Tests |
|---|---|
| Auth flow | Login form, unknown-email safety, invalid email blocked |
| Protected routes | All 7 dashboard routes redirect to /login when no cookie |
| Authenticated (skipped without `PLAYWRIGHT_AUTH_TOKEN`) | Overview KPIs, comparison search debounce, min-sites filter, alerts feed, settings, mobile nav |
