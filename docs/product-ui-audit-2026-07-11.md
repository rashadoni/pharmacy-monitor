# Pharmacy Monitor — Product & UI audit

Baseline: `58bd278f` · production and source review · RU/AZ · desktop/mobile.

## Audit health score

| Dimension | Score | Key finding |
|---|---:|---|
| Accessibility | 1/4 | Unlabelled filters/searches, non-semantic clickable table row, missing skip link, sub-44px targets |
| Performance | 3/4 | Pagination limits the matcher; analytics error handling and chart payloads need hardening |
| Responsive design | 2/4 | Layout generally fits 390px, but dense controls and touch targets are undersized |
| Theming | 3/4 | Tokens and dark palette exist; body font and native color-scheme are incomplete |
| Anti-patterns | 2/4 | Browser-serif product UI, hero metric cards and mixed-language operational copy reduce trust |
| **Total** | **11/20** | **Acceptable — significant trust and accessibility work required** |

Anti-pattern verdict: the information architecture is familiar and restrained, but the
unintentional serif rendering and English/Russian/Azerbaijani mixing make the product
look unfinished rather than intentionally designed.

## Executive summary

- P0: 0
- P1: 9
- P2: 7
- P3: 2
- Positive baseline: stable sidebar/bottom navigation, useful empty states, locale-aware
  number/date helpers, fail-closed ROI cache, responsive tables, deterministic RU/AZ
  category labels, and paginated matcher results.

## P1 findings

1. **AI confidence mixes products and matches.**
   - Location: `src/api.py` normalize stats; `frontend/src/app/[locale]/(dashboard)/overview/page.tsx`.
   - Impact: the displayed 99.8% subtracts a count of suspect Match rows from a count of
     Product rows. The percentage is dimensionally invalid and can overstate extraction quality.
   - Fix: expose products-in-review and matches-in-review separately; compute the KPI only
     from product counts.

2. **Financial recommendation provenance is not visible.**
   - Location: overview and site ROI sections.
   - Impact: the UI shows latest partial Run #444 next to recommendations materialized from
     verified full Run #443 without identifying the recommendation source.
   - Fix: expose and display the verified full-catalog run id/time.

3. **Primary RU/AZ copy mixes languages.**
   - Location: `frontend/messages/ru.json`, `frontend/messages/az.json`.
   - Examples: `Cross-site matches`, `Coverage`, `Products`, `AI confidence`, `Started`,
     `Status`, `Manual matcher`, `name+brand similarity`.
   - Impact: violates the product's explicit first-class RU/AZ contract and obscures meaning.

4. **Product UI renders in the browser's serif default.**
   - Location: `frontend/src/app/globals.css`.
   - Evidence: production desktop/mobile screenshots render headings, labels and data in serif;
     `font-sans` is configured but never applied to `body`.
   - Impact: inconsistent density, weaker scanability and visibly unfinished product UI.

5. **Locale switching loses query context.**
   - Location: `frontend/src/components/locale-switcher.tsx`.
   - Impact: switching RU/AZ on filtered comparison/matcher routes drops category, site, mode,
     and other query state, contradicting the design principle “preserve working context”.

6. **Analytics navigation is mouse-only.**
   - Location: `frontend/src/app/[locale]/(dashboard)/analytics/page.tsx` price-index rows.
   - Impact: `<tr onClick>` is not a link, cannot be opened with keyboard/Cmd-click, and has no
     semantic destination.

7. **Important form controls lack accessible names.**
   - Location: comparison filters, matcher category/search controls, alerts filters.
   - Production audit found the matcher category select and repeated search inputs without
     labels; comparison search and min-sites select are also unlabelled.

8. **Quick Actions is not an accessible menu.**
   - Location: `frontend/src/components/quick-actions.tsx`.
   - Impact: no `aria-expanded`/`aria-controls`, no Escape/focus management; click-away uses a
     clickable `<div>`; feedback is clickable rather than providing a named dismiss action.

9. **Analytics forecast hides transport/API failures.**
   - Location: `frontend/src/app/[locale]/(dashboard)/analytics/page.tsx`.
   - Impact: raw `fetch(...).then(r.json())` accepts non-2xx responses and the section has no
     actionable error state.

## P2 findings

- Comparison, alerts, categories and site catalog keep important filters in component state
  instead of the URL; reload/share/back loses working context.
- Desktop nav links are 36px high; locale buttons are 28px; matcher actions are often 20–38px.
  Mobile touch targets should be at least 44px.
- Dashboard layout has `main#main-content` but no visible-on-focus skip link.
- Bottom navigation has 6 dense items at 10px; all fit 390px but scanability is weak.
- KPI labels use tiny uppercase tracked text and generic hero-metric cards.
- `<html>` does not set `color-scheme` for dark mode native controls.
- Status/severity values such as `ok`, `degraded`, `Critical`, `Warning` are not consistently localized.

## Fix order

1. Trust metrics and recommendation provenance.
2. RU/AZ operational copy and locale query preservation.
3. Semantic navigation, labels, menu/focus behavior and skip link.
4. Analytics error handling.
5. URL-backed filter state and touch-target polish.

Re-run this audit after the P1 pass and record unresolved P2/P3 explicitly rather than
claiming full visual parity.

## Remediation pass — 2026-07-12

All nine P1 findings are addressed in the isolated implementation:

- The overview no longer calls price-spread review an AI extraction-confidence metric.
  It reports the dimensionally valid share of Product rows belonging to suspicious-price
  Match clusters; Product and Match review counts remain separate in the API.
- Recommendations and their verified full-catalog run provenance now come from one
  validated cache snapshot and one HTTP response, so labels cannot drift across runs.
- RU/AZ operational copy, the default sans font, locale query preservation, semantic
  analytics links, accessible form names, the Quick Actions menu, and analytics error/retry
  states are fixed.
- The dashboard now has a keyboard skip link, light/dark native `color-scheme`, a global
  reduced-motion fallback, and 44px mobile targets for the audited controls.
- Comparison search, minimum-site filter, sort, price-difference filter, aloe filter and
  category drill-down are URL-backed and survive locale switch, reload and sharing.

Remaining non-blocking P2/P3 work is explicit:

- Alerts, category administration and site-catalog filters still use local component state.
- The six-item mobile bottom navigation remains dense at narrow widths, although every item
  fits 390px and now has a 64px touch target.
- Some code identifiers and domain terms (`SKU`, `URL`, `is_manual`, site names) intentionally
  remain untranslated; they identify persisted concepts rather than interface prose.
- Production visual regression, keyboard traversal and failure-state smoke are deployment
  gates, not yet evidence at this pre-review stage.

Pre-deploy re-score: accessibility 4/4, performance 4/4, responsive design 3/4,
theming 4/4, anti-patterns 3/4 — **18/20**. This score remains provisional until the
production browser smoke confirms the deployed artifact.
