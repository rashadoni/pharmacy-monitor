# Canonical category architecture

## Why source categories are not the reporting model

The three catalogues describe the same products at incompatible levels:

- PharmOnline has many therapeutic and dosage-form buckets.
- AptekOnline has hundreds of narrow condition and merchandise categories.
- Aloe has a handful of broad departments such as `dermanlar`.

A direct one-row-per-source-category mapping therefore creates either hundreds
of duplicate dashboard rows or false one-to-one links. Source categories remain
the scraping routes. Reporting uses a separate compact taxonomy.

## Reporting contract

`src/category_taxonomy.py` defines 17 stable top-level categories:

1. infections and immunity;
2. respiratory system and ENT;
3. cardiovascular system and blood;
4. nervous system;
5. digestive system;
6. urogenital and reproductive health;
7. endocrinology and metabolism;
8. pain and musculoskeletal system;
9. dermatology;
10. eye health;
11. oral care;
12. vitamins, supplements and natural products;
13. mother and baby;
14. personal care, hygiene and cosmetics;
15. medical devices and equipment;
16. oncology;
17. homeopathy.

The price comparison is still built from product-identity matches. The client's
source category is mapped to this top level only after the SKU match has passed
the existing confidence, country, dead-URL and offer checks. Competitor source
categories are audit evidence; they are not treated as one-to-one truth because
the same product can legitimately be merchandised differently by another site.

Ambiguous source buckets fail closed. Dosage forms such as suppositories,
injections and solutions, generic topical/oral buckets, and Aloe's broad
`dermanlar` category do not create reporting rows.

## Current production evidence (2026-07-15)

Read-only audit command:

```bash
python -m scripts.audit_category_taxonomy --tenant-id 1
```

Production contained 3,977 live cross-site matches in 155 PharmOnline source
categories. The initial conservative rules mapped 72 source categories and
1,858 identity matches into 16 populated canonical groups. The remaining 2,119
matches stay unclassified until their source category has a defensible semantic
mapping.

Among matches where at least one competitor category is also classifiable:

| Canonical group | Alignment |
|---|---:|
| Eye health | 100.0% |
| Endocrinology and metabolism | 100.0% |
| Respiratory system and ENT | 95.7% |
| Oral care | 86.7% |
| Medical devices and equipment | 85.0% |
| Cardiovascular system and blood | 84.8% |
| Dermatology | 83.3% |
| Mother and baby | 82.1% |
| Urogenital and reproductive health | 81.7% |
| Digestive system | 78.6% |

Lower-alignment groups must not be converted into direct source-category
crosswalks without reviewing samples. They can still group already-confirmed
identical products by the client's category, which is the current screen's
contract.

## Safe extension procedure

1. Add only semantic disease/merchandise signals, never dosage-form signals.
2. Run `tests/test_category_taxonomy.py` and the production read-only audit.
3. Review conflict samples and the largest unclassified source categories.
4. Prefer leaving a category unmapped over assigning a plausible but broad
   label.
5. Recheck the category drill-down so canonical keys and legacy raw-category
   links both resolve.

The scrape configuration and the legacy `categories` table are deliberately not
deleted or collapsed by this layer.
