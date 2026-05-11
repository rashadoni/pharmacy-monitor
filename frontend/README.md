# Pharmacy Monitor — Frontend (Next.js 14)

Mobile-first dashboard для production. Заменяет Streamlit pilot.

## Quick start

```bash
cd frontend
pnpm install
pnpm dev
# → http://localhost:3000
```

Backend (FastAPI) должен работать на `:8080` параллельно (см. главный README).

В dev Next.js проксирует `/api/*` → `http://localhost:8080`. В prod Caddy сам делает этот routing.

## Структура

```
src/
  app/
    (dashboard)/        # protected — требует JWT cookie
      comparison/       # 🔍 Сравнение цен
      overview/         # 📊 Обзор (KPI + actions + runs)
      analytics/        # 📈 (TODO Week 7)
      alerts/           # 🔔 (TODO Week 8)
      watchlist/        # 📋 (TODO Week 6)
      categories/       # 🗂️ (TODO Week 8)
      settings/         # ⚙️ (TODO Week 8)
      layout.tsx        # SideNav + BottomNav + auth check
    auth/verify/        # magic-link landing
    login/              # request magic-link form
    layout.tsx          # root: html, body, providers
    page.tsx            # → redirect /overview
  components/
    nav.tsx             # SideNav (desktop) + BottomNav (mobile)
    providers.tsx       # TanStack Query
  lib/
    api.ts              # fetch wrapper + типы матчат FastAPI Pydantic
    utils.ts            # cn(), formatPrice(), formatPct()
```

## Что уже работает

- ✅ Auth flow: `/login` → email → magic-link → cookie → redirect `/overview`
- ✅ Mobile-first nav: SideNav на desktop, BottomTab на mobile
- ✅ `/overview`: 4 KPI cards + ROI actions feed + recent runs table
- ✅ `/comparison`: search + filter + mobile card / desktop table
- ✅ Theme: light/dark via CSS variables (HSL)

## TODO по плану

| Неделя | Что |
|---|---|
| W5 | Comparison E2E (search debounce, реакция на /matches/{id}/reject) |
| W6 | Watchlist CRUD UI |
| W7 | Analytics (charts через Recharts) |
| W8 | Alerts feed + Categories + Settings + i18n |
| W9 | Telegram preferences UI |
| W12 | next-intl AZ/RU/EN |

## Команды

```bash
pnpm dev              # http://localhost:3000
pnpm build            # production build
pnpm start            # serve production build
pnpm lint             # eslint + next config
pnpm typecheck        # tsc --noEmit
pnpm test             # vitest unit tests
pnpm test:e2e         # playwright e2e
pnpm format           # prettier
```

## Production build

```bash
NEXT_PUBLIC_API_URL=https://your-domain.com pnpm build
# Outputs: .next/standalone/ — self-contained, deploy через systemd
```

См. `infra/systemd/pharmacy-monitor-frontend.service` (TODO Week 4).
