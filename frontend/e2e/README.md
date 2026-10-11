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

CI does not run these scenarios. There is no e2e workflow in
`.github/workflows/`, and no job of `ci-pipeline.yml` runs `pnpm test:e2e`. Its
`Frontend (Next.js)` job runs `pnpm typecheck`, `pnpm test` (vitest, files
matching `src/**/*.test.{ts,tsx}`) and `pnpm build`. The typecheck step
type-checks the files in this directory too (`tsconfig.json` includes
`**/*.ts`), but no spec is executed there. The `Test (pytest)` job does install
Playwright's Chromium — for the Python scraper snapshot tests, not for these
specs.

A green CI therefore says nothing about what these scenarios check. Run them by
hand, as described under "Run locally", against a backend and a frontend you
started yourself.

## Coverage

| Area | Tests |
|---|---|
| Auth flow | Login form, unknown-email safety, invalid email blocked |
| Protected routes | All 7 dashboard routes redirect to /login when no cookie |
| Authenticated (skipped without `PLAYWRIGHT_AUTH_TOKEN`) | Overview KPIs, comparison search debounce, min-sites filter, alerts feed, settings, mobile nav |
