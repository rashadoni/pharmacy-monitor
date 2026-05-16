"""AI-нормализатор фармацевтических атрибутов продуктов.

Превращает сырые поля (name/brand/dosage/pack_size) в структурированные
{active_ingredient, dosage_mg, pack_count, form, brand_canonical, confidence}
через LLM-вызов (Anthropic Claude Haiku по умолчанию).

Главная ценность — matcher больше не сравнивает сырые строки. Это снимает
проблему aloe.az, где external_id — синтетический slug от названия, и любое
переформулирование на pharmonline ломало автомэтчинг.

Производительность:
- Hash-кэш: пересчёт ТОЛЬКО для продуктов где (name, brand, dosage, pack_size)
  изменились с прошлого прогона. На стабильном каталоге 95% попадают в cache.
- Батчинг 50 продуктов/вызов: LLM получает массив, возвращает массив. Снижает
  фиксированную часть стоимости (~1.5KB system prompt × 50 = окупается).
- Fail-soft: ошибка LLM/превышение бюджета → продукт остаётся с null attrs,
  matcher fall-back на legacy fuzzy путь. Не блокирует pipeline.

Стоимость на Claude Haiku ($0.25/$1.25 per 1M):
- Backfill 2087 продуктов: ~$0.04 единоразово
- Daily delta (~5500 изменённых): ~$0.11/день = ~$3/месяц
- Worst case (cache miss всё): ~$2.20/день
- Бюджет per-run AI_NORMALIZE_BUDGET_USD (default $10) — страховка от runaway.

Env vars:
- AI_NORMALIZE_PROVIDER=anthropic|openai  (default anthropic)
- AI_NORMALIZE_MODEL=claude-haiku-4-5     (default claude-haiku-4-5)
- AI_NORMALIZE_BUDGET_USD=10.0
- AI_NORMALIZE_BATCH_SIZE=50
- ANTHROPIC_API_KEY / OPENAI_API_KEY
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src._time import utcnow
from src.storage import Product

log = structlog.get_logger()


# Reuse pricing table from ai_crawler — same providers, same prices.
try:
    from src.scrapers.ai_crawler import PROVIDER_PRICING
except ImportError:  # pragma: no cover
    PROVIDER_PRICING = {
        "anthropic/claude-haiku-4-5": {"input": 0.25, "output": 1.25},
        "anthropic/claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
        "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "openai/gpt-4o": {"input": 2.50, "output": 10.00},
    }


DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_PROVIDER = "anthropic"
DEFAULT_BATCH_SIZE = 50
DEFAULT_BUDGET_USD = 10.0
LOW_CONFIDENCE_THRESHOLD = 0.7


_NORMALIZE_SYSTEM_PROMPT = """You are a pharmaceutical data normalizer for an Azerbaijani pharmacy comparison system. Three sites are compared: pharmonline.az, aptekonline.az, aloe.az.

Input: JSON array of products with fields {idx, site, name, brand?, dosage?, pack_size?}.

Output: ONLY a JSON array (same length, same idx order), each element with this exact schema:
{
  "idx": int,                          // copy from input
  "active_ingredient": string|null,    // lowercase English INN. Multi-component: "ingredient_a + ingredient_b". For supplements/cosmetics with no clear INN -> null.
  "dosage_mg": number|null,            // convert to mg. "10 ml of 5%" = 500. No dosage -> null.
  "pack_count": int|null,              // units in package. "30 tab" -> 30. "10x10 blister" -> 100. "200 ml" with unit=ml -> 200.
  "pack_unit": string|null,            // tab|capsule|ml|g|sachet|drops|pcs|other
  "form": string|null,                 // tablet|capsule|syrup|cream|gel|drops|spray|powder|sachet|injection|other
  "brand_canonical": string|null,      // official brand spelling (e.g. "GlaxoSmithKline" not "GSK"). Strip site-specific noise.
  "is_pharma": bool,                   // true for prescription/OTC drugs, false for cosmetics/supplements/devices/baby food.
  "confidence": number,                // 0..1 self-assessed
  "needs_review": bool                 // true if confidence < 0.7 OR conflicting evidence
}

Rules:
- DO NOT invent dosages or active ingredients from product names. If unsure -> null with low confidence.
- Azerbaijani names: "Asetilsalisil turşusu" = "acetylsalicylic acid". "Parasetamol" = "paracetamol".
- Russian names: respect transliteration ("Кардиомагнил" = "acetylsalicylic acid + magnesium hydroxide").
- Brand normalisation: strip product-name suffixes ("Bayer Aspirin Cardio" -> brand "Bayer", name unchanged).
- Multi-pack: "3x10 tab" -> pack_count=30, pack_unit=tab.
- Output ONLY the JSON array. No prose, no markdown, no explanations."""


@dataclass
class NormalizeStats:
    """Sводный результат прогона ai_normalize."""

    products_total: int = 0
    products_called: int = 0
    products_cached: int = 0
    products_failed: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    budget_exceeded: bool = False
    failures: list[str] = field(default_factory=list)


# ─── Hash computation ────────────────────────────────────────────────────────


def compute_normalize_hash(
    site: str,
    name: str,
    brand: str | None,
    dosage: str | None,
    pack_size: str | None,
) -> str:
    """sha256 канонизированных входных полей. Стабильно, регистро-независимо."""
    payload = "|".join(
        (
            (site or "").lower().strip(),
            (name or "").lower().strip(),
            (brand or "").lower().strip(),
            (dosage or "").lower().strip(),
            (pack_size or "").lower().strip(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── Provider pricing ────────────────────────────────────────────────────────


def estimate_cost_usd(tokens_in: int, tokens_out: int, provider: str, model: str) -> float:
    """Стоимость на основе зафиксированной таблицы PROVIDER_PRICING."""
    key = f"{provider}/{model}"
    pricing = PROVIDER_PRICING.get(key, {"input": 0.25, "output": 1.25})
    return (tokens_in / 1_000_000) * pricing["input"] + (
        tokens_out / 1_000_000
    ) * pricing["output"]


# ─── LLM batch call ──────────────────────────────────────────────────────────


def _build_user_prompt(products: list[dict]) -> str:
    """Сериализация батча в компактный JSON для user-message."""
    return json.dumps(products, ensure_ascii=False, separators=(",", ":"))


def _parse_llm_response(text: str) -> list[dict]:
    """Извлечь JSON-array из ответа LLM. Tolerant к markdown-fence."""
    # Strip ```json fences and any surrounding prose
    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON array in LLM response")
    return json.loads(m.group(0))


def _call_anthropic(batch: list[dict], model: str) -> tuple[list[dict], int, int]:
    """Sync wrapper для Anthropic Messages API. Возвращает (attrs, tokens_in, tokens_out)."""
    from anthropic import Anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=4000,  # 50 продуктов × ~50 токенов ответа = 2500, запас 4000
        system=_NORMALIZE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(batch)}],
    )
    in_tok = resp.usage.input_tokens
    out_tok = resp.usage.output_tokens
    text = resp.content[0].text if resp.content else "[]"
    attrs = _parse_llm_response(text)
    return attrs, in_tok, out_tok


def _call_openai(batch: list[dict], model: str) -> tuple[list[dict], int, int]:
    """Sync wrapper для OpenAI Chat API."""
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _NORMALIZE_SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(batch)},
        ],
        response_format={"type": "json_object"},
        max_tokens=4000,
        temperature=0,
    )
    in_tok = resp.usage.prompt_tokens if resp.usage else 0
    out_tok = resp.usage.completion_tokens if resp.usage else 0
    text = resp.choices[0].message.content or "[]"
    # OpenAI json_object не позволяет array на верхнем уровне → завернём в obj
    # либо вернём чистый array если модель так и отдала
    try:
        data = json.loads(text)
        attrs = data if isinstance(data, list) else data.get("products") or []
    except json.JSONDecodeError:
        attrs = _parse_llm_response(text)
    return attrs, in_tok, out_tok


def call_llm_batch(
    batch: list[dict], *, provider: str, model: str
) -> tuple[list[dict], int, int]:
    """Dispatch по провайдеру. Выкидывает исключение на любую проблему — caller решает."""
    if provider == "anthropic":
        return _call_anthropic(batch, model)
    if provider == "openai":
        return _call_openai(batch, model)
    raise RuntimeError(f"unknown AI_NORMALIZE_PROVIDER={provider!r}")


# ─── Cache lookup ────────────────────────────────────────────────────────────


def _load_cached_attrs_by_hash(
    session: Session, hashes: list[str]
) -> dict[str, dict]:
    """Найти готовые normalized_attrs по hash — для переиспользования между прогонами.

    Возвращает {hash: normalized_attrs}. Берём ЛЮБОЙ продукт с этим hash'ом
    (они эквивалентны по semantically важным полям).
    """
    if not hashes:
        return {}
    rows = session.execute(
        select(Product.normalize_hash, Product.normalized_attrs).where(
            Product.normalize_hash.in_(hashes),
            Product.normalized_attrs.is_not(None),
        )
    ).all()
    out: dict[str, dict] = {}
    for h, attrs in rows:
        if h and attrs and h not in out:
            out[h] = attrs
    return out


# ─── Persist ─────────────────────────────────────────────────────────────────


def _save_attrs(
    session: Session, product_id: int, attrs: dict, hash_key: str
) -> None:
    """Записать attrs + hash + timestamp. Один UPDATE."""
    session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(
            normalized_attrs=attrs,
            normalize_hash=hash_key,
            normalized_at=utcnow(),
        )
    )


def _decorate_attrs(raw: dict) -> dict:
    """Дописать needs_review/extracted_at, нормализовать типы."""
    out = dict(raw)
    out.pop("idx", None)
    conf = out.get("confidence")
    try:
        conf = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = max(0.0, min(1.0, conf))
    out["needs_review"] = bool(
        out.get("needs_review", False) or conf < LOW_CONFIDENCE_THRESHOLD
    )
    out["extracted_at"] = utcnow().isoformat()
    return out


# ─── Main entry point ────────────────────────────────────────────────────────


def _pending_products(
    session: Session,
    *,
    site: str | None,
    limit: int | None,
    force: bool,
) -> list[Product]:
    """Продукты которым нужна нормализация (новые ИЛИ изменили hash ИЛИ force)."""
    stmt = select(Product)
    if site is not None:
        stmt = stmt.where(Product.site == site)
    products = list(session.scalars(stmt).all())

    pending: list[Product] = []
    for p in products:
        new_hash = compute_normalize_hash(p.site, p.name, p.brand, p.dosage, p.pack_size)
        if force or p.normalize_hash != new_hash or p.normalized_attrs is None:
            pending.append(p)
        if limit is not None and len(pending) >= limit:
            break
    return pending


def normalize_run(
    session: Session,
    *,
    site: str | None = None,
    limit: int | None = None,
    batch_size: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    budget_usd: float | None = None,
    force: bool = False,
) -> NormalizeStats:
    """Основной API. Нормализует все pending продукты, возвращает статистику.

    - `site` — ограничить одним сайтом (pharmonline/aptekonline/aloe)
    - `limit` — максимум продуктов в этом прогоне (smoke-тесты)
    - `force` — пересчитать даже cached (после изменения prompt'а)
    """
    provider = provider or os.getenv("AI_NORMALIZE_PROVIDER", DEFAULT_PROVIDER)
    model = model or os.getenv("AI_NORMALIZE_MODEL", DEFAULT_MODEL)
    batch_size = batch_size or int(
        os.getenv("AI_NORMALIZE_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))
    )
    budget_usd = (
        budget_usd
        if budget_usd is not None
        else float(os.getenv("AI_NORMALIZE_BUDGET_USD", str(DEFAULT_BUDGET_USD)))
    )

    stats = NormalizeStats()
    pending = _pending_products(session, site=site, limit=limit, force=force)
    stats.products_total = len(pending)
    if not pending:
        log.info("ai_normalize_nothing_to_do", site=site)
        return stats

    # Step 1: hash-cache lookup (skip force mode — каждый продукт идёт в LLM)
    cache_map: dict[str, dict] = {}
    new_hashes: dict[int, str] = {}
    for p in pending:
        h = compute_normalize_hash(p.site, p.name, p.brand, p.dosage, p.pack_size)
        new_hashes[p.id] = h
    if not force:
        cache_map = _load_cached_attrs_by_hash(
            session, list({h for h in new_hashes.values()})
        )

    # Step 2: applying cached attrs (no LLM call)
    cached_ids: set[int] = set()
    for p in pending:
        h = new_hashes[p.id]
        if h in cache_map:
            _save_attrs(session, p.id, cache_map[h], h)
            cached_ids.add(p.id)
            stats.products_cached += 1
    if cached_ids:
        session.commit()

    # Step 3: batch LLM call for the rest
    to_call = [p for p in pending if p.id not in cached_ids]
    log.info(
        "ai_normalize_batches_planned",
        total=stats.products_total,
        cached=stats.products_cached,
        to_call=len(to_call),
        batches=(len(to_call) + batch_size - 1) // batch_size,
        provider=provider,
        model=model,
        budget_usd=budget_usd,
    )

    for batch_start in range(0, len(to_call), batch_size):
        batch_products = to_call[batch_start : batch_start + batch_size]
        # Pre-flight budget check
        if stats.cost_usd >= budget_usd:
            stats.budget_exceeded = True
            log.warning(
                "ai_normalize_budget_exceeded",
                cost=stats.cost_usd,
                remaining=len(to_call) - batch_start,
            )
            break

        payload = [
            {
                "idx": i,
                "site": p.site,
                "name": p.name,
                "brand": p.brand,
                "dosage": p.dosage,
                "pack_size": p.pack_size,
            }
            for i, p in enumerate(batch_products)
        ]

        try:
            attrs_list, in_tok, out_tok = call_llm_batch(
                payload, provider=provider, model=model
            )
            stats.tokens_in += in_tok
            stats.tokens_out += out_tok
            stats.cost_usd = estimate_cost_usd(
                stats.tokens_in, stats.tokens_out, provider, model
            )
        except Exception as e:
            stats.products_failed += len(batch_products)
            stats.failures.append(f"batch_{batch_start}: {e}")
            log.warning(
                "ai_normalize_batch_failed",
                batch_start=batch_start,
                size=len(batch_products),
                error=str(e),
            )
            continue

        # Map by idx in case LLM reorders
        by_idx = {a.get("idx"): a for a in attrs_list if isinstance(a, dict)}
        for i, p in enumerate(batch_products):
            raw = by_idx.get(i)
            if raw is None:
                stats.products_failed += 1
                stats.failures.append(f"missing_idx_{i}_in_batch_{batch_start}")
                continue
            attrs = _decorate_attrs(raw)
            _save_attrs(session, p.id, attrs, new_hashes[p.id])
            stats.products_called += 1

        session.commit()
        log.info(
            "ai_normalize_batch_done",
            batch=batch_start // batch_size + 1,
            cost=round(stats.cost_usd, 4),
            tokens_in=stats.tokens_in,
            tokens_out=stats.tokens_out,
        )

    log.info("ai_normalize_run_done", **_stats_to_dict(stats))
    return stats


def _stats_to_dict(s: NormalizeStats) -> dict[str, Any]:
    return {
        "products_total": s.products_total,
        "products_called": s.products_called,
        "products_cached": s.products_cached,
        "products_failed": s.products_failed,
        "tokens_in": s.tokens_in,
        "tokens_out": s.tokens_out,
        "cost_usd": round(s.cost_usd, 4),
        "budget_exceeded": s.budget_exceeded,
    }
