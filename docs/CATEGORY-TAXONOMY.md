# Canonical category architecture

## Why source categories are not the reporting model

The three catalogues describe the same products at incompatible levels:

- PharmOnline has many therapeutic and dosage-form buckets (178 live).
- AptekOnline has hundreds of narrow condition and merchandise categories (283 live).
- Aloe has a handful of broad departments; `dermanlar` alone holds 5,485 products.

A direct one-row-per-source-category mapping therefore creates either hundreds
of duplicate dashboard rows or false one-to-one links. Source categories remain
the scraping routes. Reporting uses a separate compact taxonomy.

## Reporting contract

`src/category_taxonomy.py` defines 17 stable top-level categories: infections and
immunity; respiratory and ENT; cardiovascular and blood; nervous system;
digestive; urogenital and reproductive; endocrinology and metabolism; pain and
musculoskeletal; dermatology; eye health; oral care; vitamins and supplements;
mother and baby; personal care; medical devices; oncology; homeopathy.

The price comparison is still built from **product-identity matches**. The
client's source category is mapped to this top level only after the SKU match has
passed the existing confidence, country, dead-URL and offer checks. Competitor
source categories are **audit evidence only** — never a one-to-one truth (see
"Why competitor categories are not a crosswalk").

Ambiguous source buckets fail closed. Dosage forms (suppositories, injections,
`peroral vasiteler`, potency classes) and Aloe's broad departments do not create
reporting rows.

## Classifier design (why it is built this way)

Four properties are load-bearing. Each replaced a defect proven against
production data on 2026-07-15; the regression tests in
`tests/test_category_taxonomy.py` pin all of them.

1. **Order independence.** Rules are collected, then resolved — never
   first-match-wins. Previously the generic `respirator` (mask) device signal
   swallowed `respirator-distress-sindromu`, a *disease*, purely because
   `medical_devices` was listed before `respiratory_ent`. Dominance is now
   explicit via `Rule.supersedes`.
2. **One spelling.** PharmOnline slugs transliterate Azerbaijani with digraphs
   (`şampunlar` → `shampunlar`); AptekOnline labels keep diacritics, and
   `strip_accents` folds `ş`→`s`. Signals written as `shampun` matched the slug
   but never the label, so AptekOnline "Шампуни" (613 products) went unmapped
   while PharmOnline `shampunlar` mapped — the same concept, a different answer
   per site. Both spellings are now folded to one canonical form.
3. **Word-boundary modes.** `mode='prefix'` tolerates agglutination
   (`agiz boslug` matches `ağız boşluğunun`); `mode='word'` is required for short
   signals — `bad` (БАД) as a prefix would also match `badam` (almond).
4. **Per-field matching.** Slug and labels are matched separately so a phrase can
   never span the seam between two unrelated fields.

`rule_validation_problems()` rejects static defects that would reintroduce
order-dependence — most importantly the same phrase claimed by two canonical
groups (`beyin qan` was in both `cardiovascular_blood` and `nervous_system`).
`scripts/audit_category_taxonomy.py --strict` exits non-zero on these.

### The one segment policy

The taxonomy mixes a body-system axis with a single customer-segment group
(`mother_baby`). Both legitimately fire for "kids' oral care". The segment wins,
by explicit policy, because the client's own tree merchandises kids buckets as
`ushaq-*` (kids' skin care, kids' hair care). Before this was explicit, order
decided silently and inconsistently: kids' oral care landed in `oral_care` while
kids' skin care landed in `mother_baby`.

This policy is a **judgement call, not a fact**. Every category it touches is
listed under `segment_policy_source_categories` in the audit (8 categories in
production) so it stays reviewable and can be flipped in one place
(`_SEGMENT_KEY`).

## Why competitor categories are not a crosswalk

Measured on production, restricted to **confirmed identity matches** where both
the client and a competitor category classify:

| Canonical group | Aligned | Conflicting | Alignment |
|---|---:|---:|---:|
| Eye health | 48 | 0 | 100.0% |
| Medical devices | 17 | 0 | 100.0% |
| Endocrinology and metabolism | 76 | 2 | 97.4% |
| Respiratory and ENT | 223 | 10 | 95.7% |
| Cardiovascular and blood | 262 | 44 | 85.6% |
| Dermatology | 10 | 2 | 83.3% |
| Mother and baby | 48 | 10 | 82.8% |
| Urogenital and reproductive | 143 | 32 | 81.7% |
| Personal care | 35 | 9 | 79.5% |
| Digestive | 154 | 41 | 79.0% |
| Nervous system | 41 | 18 | 69.5% |
| Oral care | 8 | 5 | 61.5% |
| Oncology | 2 | 3 | 40.0% |
| Pain and musculoskeletal | 28 | 53 | 34.6% |
| **Vitamins and supplements** | 4 | 66 | **5.7%** |
| **Infections and immunity** | 0 | 81 | **0.0%** |

The bottom rows are the point. For products the matcher has **confirmed to be
identical**, PharmOnline files "инфекционно-воспалительные заболевания кожи"
under infection while competitors file the same SKU under skin. Neither is
wrong — they are different merchandising axes. This is measured proof that a
source-category → source-category crosswalk is **not supported by the data**, and
why grouping is done on the client's category only, after identity is confirmed.

Do not convert a high-alignment row into a direct crosswalk either: 100% on eye
health is 48 SKUs agreeing, not evidence that the two catalogues mean the same
thing in general.

## Current production evidence (2026-07-15, read-only)

```bash
python -m scripts.audit_category_taxonomy --tenant-id 1 --strict \
  > artifacts/category-taxonomy-audit.json
```

Source-category coverage across all 466 live `(site, category)` pairs:

| Site | Categories mapped | Products mapped |
|---|---:|---:|
| pharmonline (client) | 86 / 178 | 5,965 / 10,480 (56.9%) |
| aptekonline | 186 / 283 | 21,040 / 26,480 (79.5%) |
| aloe | 2 / 5 | 679 / 6,363 (10.7%) |

Aloe is low **by design**: `dermanlar` (5,485 products) is a whole-catalogue
department and is blocked.

Classification outcomes: 266 matched, 145 no signal, 44 blocked as dosage form,
8 resolved by segment policy, 2 blocked as broad buckets, **1 ambiguous**
(AptekOnline 403 "Косметические контактные линзы" — `medical_devices` vs
`personal_care`, genuinely both, correctly left unmapped).

The dashboard renders **16 canonical rows from 148 raw client categories**,
grouping 1,558 confirmed matched SKUs. 2,032 of 3,977 cross-site matches stay
unclassified because their client category has no defensible mapping.

## Safe extension procedure

1. Add only semantic disease/merchandise signals, never dosage-form signals.
2. Write phrases in canonical folded form (no `sh`/`ch`/`gh` digraphs) — the
   validator rejects the rest.
3. Use `mode='word'` for any signal under ~5 characters.
4. Run `pytest tests/test_category_taxonomy.py` and the read-only production
   audit; `--strict` must stay green.
5. Review `ambiguous_source_categories` and the largest `no_signal` categories.
6. Prefer leaving a category unmapped over assigning a plausible but broad label.

The scrape configuration and the legacy `categories` table are deliberately not
deleted or collapsed by this layer.

## Known follow-ups (not fixed here)

- **`infectious_immune` 0% / `vitamins_supplements` 5.7% alignment.** The client
  classifies organ-infections by pathogen while competitors classify by organ.
  Worth a product decision on which axis the client wants — but it must be a
  decision, not a silent rule tweak.
- **`catalog_verified` is false for every production run** while 5 runs report
  `run_quality.financially_eligible=true`. Until that reconciles, financial
  output relies on the bootstrap fallback documented in `_iter_matched_prices`.
