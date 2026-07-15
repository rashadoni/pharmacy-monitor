"""Audit canonical category coverage and cross-site agreement.

Read-only.  The report distinguishes categories that all classifiable source
sites agree on from conflicts and broad/unclassifiable source buckets.  It is
intended to be run before expanding taxonomy rules.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict

import click
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from src.category_taxonomy import (
    Classification,
    classify_source_category_detailed,
    rule_validation_problems,
    source_category_labels,
)
from src.storage import Match, Product, make_session


def build_category_taxonomy_audit(session, *, tenant_id: int = 1) -> dict:
    labels = source_category_labels(session)
    matches = session.scalars(
        select(Match).where(Match.tenant_id == tenant_id).options(selectinload(Match.products))
    ).all()

    raw_client_categories: set[str] = set()
    mapped_client_categories: set[str] = set()
    unclassified_client: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    aligned: Counter[str] = Counter()
    unverifiable: Counter[str] = Counter()
    conflicting: Counter[str] = Counter()
    raw_by_canonical: dict[str, set[str]] = defaultdict(set)
    conflict_samples: list[dict] = []

    # Классификация зависит только от (site, category) — мемоизируем, иначе на
    # каждый товар каждого матча прогоняется весь набор правил.
    _classified: dict[tuple[str, str], Classification] = {}

    def classify(site: str, category: str | None) -> Classification:
        cache_key = (site, category or "")
        if cache_key not in _classified:
            label_ru, label_az = labels.get(cache_key, (None, None))
            _classified[cache_key] = classify_source_category_detailed(
                site, category, label_ru=label_ru, label_az=label_az
            )
        return _classified[cache_key]

    def mapped_key(product) -> str | None:
        result = classify(product.site, product.category)
        return result.category.key if result.category else None

    for match in matches:
        live = [product for product in match.products if product.url_dead_at is None]
        clients = [product for product in live if product.site == "pharmonline"]
        competitors = [product for product in live if product.site != "pharmonline"]
        if not clients or not competitors:
            continue

        client = clients[0]
        # Сентинел НЕ считаем реальной категорией — иначе raw_client_categories
        # завышается на 1 при первом же товаре без категории.
        raw_category = client.category or "(uncategorized)"
        if client.category:
            raw_client_categories.add(raw_category)
        client_key = mapped_key(client)
        if client_key is None:
            unclassified_client[raw_category] += 1
            continue

        mapped_client_categories.add(raw_category)
        raw_by_canonical[client_key].add(raw_category)
        totals[client_key] += 1

        competitor_keys = {key for product in competitors if (key := mapped_key(product))}
        if not competitor_keys:
            unverifiable[client_key] += 1
        elif competitor_keys == {client_key}:
            aligned[client_key] += 1
        else:
            conflicting[client_key] += 1
            if len(conflict_samples) < 30:
                conflict_samples.append(
                    {
                        "match_id": match.id,
                        "name": match.canonical_name,
                        "client_category": raw_category,
                        "client_canonical": client_key,
                        "competitor_canonical": sorted(competitor_keys),
                    }
                )

    category_rows = []
    for key, total in totals.most_common():
        checked = aligned[key] + conflicting[key]
        category_rows.append(
            {
                "key": key,
                "matched_skus": total,
                "source_categories": len(raw_by_canonical[key]),
                "aligned_skus": aligned[key],
                "conflicting_skus": conflicting[key],
                "unverifiable_skus": unverifiable[key],
                "alignment_pct": round(aligned[key] / checked * 100, 1) if checked else None,
            }
        )

    # Полная инвентаризация КАЖДОЙ категории-источника, а не только матченных:
    # покрытие по сайтам + причина, по которой категория осталась несопоставленной.
    # Тот же tenant + только живые товары, что и в блоке матчей выше — иначе две
    # половины отчёта считают разные популяции.
    inventory_rows = session.execute(
        select(Product.site, Product.category, func.count(Product.id))
        .where(
            Product.category.is_not(None),
            Product.tenant_id == tenant_id,
            Product.url_dead_at.is_(None),
        )
        .group_by(Product.site, Product.category)
    ).all()

    coverage: dict[str, Counter[str]] = defaultdict(Counter)
    reasons: Counter[str] = Counter()
    ambiguous_rows: list[dict] = []
    segment_policy_rows: list[dict] = []
    # `ambiguous`/`segment_policy` only surface TIES. A confidently-wrong
    # single-signal `matched` lands in no queue at all — that is exactly how
    # `usaq` (child) matching `uşaqlıq` (uterus) survived a full prod audit.
    # Grouping mapped categories by the rule that carried them makes a
    # low-specificity signal quietly owning many categories visible.
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for site, category, product_count in inventory_rows:
        result = classify(site, category)
        reasons[result.reason] += 1
        coverage[site]["categories"] += 1
        coverage[site]["products"] += int(product_count)
        if result.category is not None:
            coverage[site]["mapped_categories"] += 1
            coverage[site]["mapped_products"] += int(product_count)
        row = {
            "site": site,
            "category": category,
            "products": int(product_count),
            "candidates": list(result.candidates),
            "rule_ids": list(result.rule_ids),
        }
        if result.category is not None:
            for rule_id in result.rule_ids:
                by_rule[rule_id].append(
                    {"site": site, "category": category, "products": int(product_count)}
                )
        if result.reason == "ambiguous":
            ambiguous_rows.append(row)
        elif result.reason == "segment_policy":
            segment_policy_rows.append(row)

    return {
        # Дефекты набора правил (дубль фразы между категориями и т.п.) сделали бы
        # классификацию зависимой от порядка. Пусто = набор корректен.
        "policy_defects": rule_validation_problems(),
        "summary": {
            "cross_site_matches": sum(totals.values()) + sum(unclassified_client.values()),
            "raw_client_categories": len(raw_client_categories),
            "mapped_raw_client_categories": len(mapped_client_categories),
            "canonical_categories": len(totals),
            "mapped_matches": sum(totals.values()),
            "unclassified_matches": sum(unclassified_client.values()),
            "aligned_matches": sum(aligned.values()),
            "conflicting_matches": sum(conflicting.values()),
            "unverifiable_matches": sum(unverifiable.values()),
        },
        "source_coverage": {
            site: {
                "categories": counts["categories"],
                "mapped_categories": counts["mapped_categories"],
                "products": counts["products"],
                "mapped_products": counts["mapped_products"],
                "mapped_products_pct": (
                    round(counts["mapped_products"] / counts["products"] * 100, 1)
                    if counts["products"]
                    else None
                ),
            }
            for site, counts in sorted(coverage.items())
        },
        "classification_reasons": dict(sorted(reasons.items())),
        # Review surface for confidently-wrong mappings (see `by_rule` above):
        # a broad signal owning an unexpected pile of categories is the smell.
        "mapped_categories_by_rule": [
            {
                "rule_id": rule_id,
                "categories": len(rows),
                "products": sum(r["products"] for r in rows),
                "sample": sorted(rows, key=lambda r: -r["products"])[:5],
            }
            for rule_id, rows in sorted(by_rule.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        # Обе категории сработали, ничего не разрешило ничью → остаётся
        # несопоставленной. Это и есть очередь на ручной разбор.
        "ambiguous_source_categories": sorted(ambiguous_rows, key=lambda r: -r["products"]),
        # Сработала документированная segment-политика (детское → «Мама и ребёнок»).
        "segment_policy_source_categories": sorted(
            segment_policy_rows, key=lambda r: -r["products"]
        ),
        "categories": category_rows,
        "top_unclassified_client_categories": [
            {"category": category, "matched_skus": count}
            for category, count in unclassified_client.most_common(30)
        ],
        "conflict_samples": conflict_samples,
    }


@click.command()
@click.option("--tenant-id", default=1, type=int, show_default=True)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero if the rule set has static defects (for CI / pre-deploy).",
)
def main(tenant_id: int, strict: bool) -> None:
    """Print a read-only JSON audit to stdout."""
    session = make_session()()
    try:
        report = build_category_taxonomy_audit(session, tenant_id=tenant_id)
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
        # Только СТАТИЧЕСКИЕ дефекты правил валят --strict. Неоднозначность в
        # проде — нормальный рабочий выход (её и надо разбирать), не сбой.
        if strict and report["policy_defects"]:
            raise SystemExit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
