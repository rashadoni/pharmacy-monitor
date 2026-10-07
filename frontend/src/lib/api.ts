/**
 * API client. All requests are same-origin in prod (Caddy proxies /api → FastAPI).
 * In dev, Next.js rewrites /api/* to localhost:8080 (see next.config.mjs).
 *
 * Auth: JWT cookie (httpOnly, set by /auth/verify endpoint). No manual token handling.
 *
 * Phase 5.5 (2026-05-27): correlation via X-Request-ID. Каждый fetch генерит
 * uuid'ом client-side; backend либо принимает наш, либо генерит свой; в обоих
 * случаях возвращает в response header — мы пишем его в ApiError, чтобы
 * support мог сопоставить с server-side логом / Sentry трейсом.
 */
const BASE = ""; // same origin

/** Generate RFC4122-ish v4 UUID — для трассировки запросов через стек. */
function genRequestId(): string {
  // Browser-native (Safari 15.4+, Chrome 92+, Firefox 95+).
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  // Fallback (legacy IE, server-side build).
  return "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx".replace(/x/g, () =>
    Math.floor(Math.random() * 16).toString(16),
  );
}

async function request<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  const timeoutMs = init?.timeoutMs ?? 30_000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const requestId = genRequestId();
  try {
    const res = await fetch(`${BASE}${path}`, {
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-Request-ID": requestId,
        ...(init?.headers || {}),
      },
      signal: controller.signal,
      ...init,
    });
    // Backend echoes our X-Request-ID (or substitutes its own).
    const responseRequestId = res.headers.get("X-Request-ID") || requestId;
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new ApiError(res.status, text || res.statusText, responseRequestId);
    }
    if (res.status === 204) return undefined as T;
    return res.json();
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(
        408,
        `Запрос дольше ${Math.round(timeoutMs / 1000)}с — сервер не ответил`,
        requestId,
      );
    }
    if (err instanceof ApiError) throw err;
    // Network failure / DNS / refused — wrap with request_id для трассировки.
    if (err instanceof TypeError) {
      throw new ApiError(0, `Сеть недоступна: ${err.message}`, requestId);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    public requestId?: string,
  ) {
    super(`API ${status}: ${detail}`);
  }
}

const VERIFIED_SCAN_PENDING_DETAIL =
  "Verified full-catalog recommendations are not available yet";
const RECOMMENDATIONS_RECALCULATING_DETAIL = "Recommendations are being recalculated";

function isUnavailableWithDetail(err: unknown, detail: string): err is ApiError {
  if (!(err instanceof ApiError) || err.status !== 503) return false;

  if (err.detail === detail) return true;
  try {
    const payload = JSON.parse(err.detail);
    return payload?.detail === detail;
  } catch {
    return false;
  }
}

/** Expected fail-closed state while financial lineage awaits a verified full scan. */
export function isVerifiedScanPendingError(err: unknown): err is ApiError {
  return isUnavailableWithDetail(err, VERIFIED_SCAN_PENDING_DETAIL);
}

/**
 * Thresholds or purchase costs were just changed: the old recommendations are
 * withdrawn and the server is recomputing them (minutes, no scan involved).
 */
export function isRecommendationsRecalculatingError(err: unknown): err is ApiError {
  return isUnavailableWithDetail(err, RECOMMENDATIONS_RECALCULATING_DETAIL);
}

const ROI_RECALCULATION_POLL_MS = 20_000;

/**
 * `refetchInterval` for the recommendations query: poll while a recalculation
 * is queued so the result appears without a reload, stay quiet otherwise.
 */
export function roiRecalculationPollMs(state: {
  data?: RoiRecommendations;
  error: unknown;
}): number | false {
  if (isRecommendationsRecalculatingError(state.error)) return ROI_RECALCULATION_POLL_MS;
  if (state.data?.provenance.refresh_pending) return ROI_RECALCULATION_POLL_MS;
  return false;
}

/** True when financial views are intentionally paused by the fail-closed gate. */
export function isFullCatalogTrustError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 503 &&
    error.detail.includes("full_catalog_trust_not_ready")
  );
}

/**
 * Преобразовать ApiError / Error в человекочитаемое сообщение для UI.
 * Анализирует Pydantic 422 (validation), 401/403/404/409 и timeout 408.
 */
type UiLocale = "ru" | "az" | "en";

const ERROR_COPY: Record<
  UiLocale,
  {
    field: string;
    login: string;
    forbidden: string;
    notFound: string;
    timeout: string;
    network: string;
    server: string;
    conflict: string;
    error: string;
    unknown: string;
  }
> = {
  ru: {
    field: "поле",
    login: "Нужно войти заново",
    forbidden: "Нет прав доступа (только администраторы)",
    notFound: "Не найдено",
    timeout: "Сервер не ответил вовремя. Попробуйте ещё раз.",
    network: "Сеть недоступна. Проверьте подключение.",
    server: "Сервер недоступен. Попробуйте через минуту.",
    conflict: "Конфликт данных. Обновите страницу и проверьте изменения.",
    error: "Ошибка",
    unknown: "Неизвестная ошибка",
  },
  az: {
    field: "sahə",
    login: "Yenidən daxil olun",
    forbidden: "Giriş icazəsi yoxdur (yalnız administratorlar)",
    notFound: "Tapılmadı",
    timeout: "Server vaxtında cavab vermədi. Yenidən cəhd edin.",
    network: "Şəbəkə əlçatan deyil. Bağlantını yoxlayın.",
    server: "Server əlçatan deyil. Bir dəqiqə sonra cəhd edin.",
    conflict:
      "Məlumat ziddiyyəti var. Səhifəni yeniləyib dəyişiklikləri yoxlayın.",
    error: "Xəta",
    unknown: "Naməlum xəta",
  },
  en: {
    field: "field",
    login: "Please sign in again",
    forbidden: "Access denied (administrators only)",
    notFound: "Not found",
    timeout: "The server did not respond in time. Try again.",
    network: "The network is unavailable. Check your connection.",
    server: "The server is unavailable. Try again in a minute.",
    conflict: "Data conflict. Refresh the page and review your changes.",
    error: "Error",
    unknown: "Unknown error",
  },
};

function normalizeUiLocale(locale: string): UiLocale {
  return locale === "az" || locale === "en" ? locale : "ru";
}

export function friendlyError(err: unknown, locale = "ru"): string {
  const normalizedLocale = normalizeUiLocale(locale);
  const copy = ERROR_COPY[normalizedLocale];
  if (err instanceof ApiError) {
    // Pydantic 422 detail обычно JSON: [{loc, msg, type}, ...]
    if (err.status === 422 && err.detail.startsWith("{")) {
      try {
        const data = JSON.parse(err.detail);
        if (Array.isArray(data?.detail)) {
          const first = data.detail[0];
          if (first?.msg) {
            const field = first.loc?.slice(-1)?.[0] ?? copy.field;
            return `${field}: ${translatePydantic(first.msg, normalizedLocale)}`;
          }
        }
      } catch {
        /* fall through */
      }
    }
    if (err.status === 0) return copy.network;
    if (err.status === 401) return copy.login;
    if (err.status === 403) return copy.forbidden;
    if (err.status === 404) return copy.notFound;
    if (err.status === 408) return copy.timeout;
    if (err.status === 409) {
      if (normalizedLocale !== "ru") return copy.conflict;
      // 409 detail обычно уже на русском от backend
      try {
        const d = JSON.parse(err.detail);
        return d.detail ?? err.detail;
      } catch {
        return err.detail;
      }
    }
    if (err.status >= 500) return copy.server;
    if (err.status >= 400 && normalizedLocale !== "ru") {
      return `${copy.error} ${err.status}`;
    }
    return err.detail || `${copy.error} ${err.status}`;
  }
  if (err instanceof Error) return err.message;
  return copy.unknown;
}

/**
 * Phase 5.5 (2026-05-27): retrieve X-Request-ID для отображения в UI или
 * передачи в Sentry. `null` если ошибка не ApiError или request_id не пришёл.
 *
 * Usage:
 *   const id = getRequestId(err);
 *   if (id) Sentry.setTag("request_id", id);
 *   showToast(`Ошибка ${friendlyError(err)} (id: ${id})`);
 */
export function getRequestId(err: unknown): string | null {
  if (err instanceof ApiError && err.requestId) return err.requestId;
  return null;
}

function translatePydantic(msg: string, locale: UiLocale): string {
  const map: Record<UiLocale, Record<string, string>> = {
    ru: {
      "Field required": "обязательное поле",
      "Input should be a valid email address": "введите корректный email",
      "String should have at least 3 characters": "минимум 3 символа",
      "value is not a valid integer": "должно быть числом",
    },
    az: {
      "Field required": "məcburi sahə",
      "Input should be a valid email address":
        "düzgün e-poçt ünvanı daxil edin",
      "String should have at least 3 characters": "ən azı 3 simvol olmalıdır",
      "value is not a valid integer": "tam ədəd olmalıdır",
    },
    en: {
      "Field required": "required field",
      "Input should be a valid email address": "enter a valid email address",
      "String should have at least 3 characters":
        "must contain at least 3 characters",
      "value is not a valid integer": "must be an integer",
    },
  };
  return map[locale][msg] ?? msg;
}

// ─── Types matching FastAPI Pydantic schemas ───────────────────────────────

export interface MeOut {
  id: number;
  email: string;
  name: string | null;
  role: string;
  tenant_id: number;
}

export interface ComparisonRow {
  canonical_id: number;
  name: string;
  brand: string | null;
  pack_size: string | null;
  is_manual: boolean;
  sites_with_price: number;
  min_price: number | null;
  max_price: number | null;
  spread_pct: number | null;
  cheapest_site: string | null;
  prices: Record<
    string,
    {
      price: number;
      is_on_sale: boolean;
      url: string;
      product_id: number;
      country_code?: string | null;
      country_resolution_status?:
        | "resolved"
        | "unknown"
        | "ambiguous"
        | "invalid";
      availability_status?: "in_stock" | "out_of_stock" | "unknown";
      availability_observed_at?: string | null;
      // Per-unit normalization (2026-05-29). pack_count = штук в упаковке,
      // unit_price = price/pack_count. Заполняются всегда; используются для
      // отображения когда spread_basis === "unit".
      pack_count?: number;
      unit_price?: number;
      pack_size?: string | null;
      // Свежесть (2026-05-29). age_days = дней с last_seen_at; stale=true когда
      // цена старше порога (14д) — показывается с бейджем «N дн. назад» и НЕ
      // участвует в расчёте spread (устаревшая цена не даёт ложный undercut).
      age_days?: number | null;
      stale?: boolean;
    }
  >;
  confidence: number;
  needs_review: boolean;
  /** "unit" когда spread/cheapest посчитаны на цене-за-штуку (разные фасовки). */
  spread_basis?: "raw" | "unit";
}

export interface PricingConfig {
  raise_threshold_pct: number;
  undercut_threshold_pct: number;
  max_spread_pct: number;
  min_margin_pct: number;
  max_per_type: number;
  updated_at?: string | null;
}

export interface CostImportResult {
  batch_id: number | null;
  rows_processed: number;
  rows_imported: number;
  rows_skipped: number;
  errors: string[];
}

export interface CostImportPreview extends CostImportResult {
  changes: {
    line: number;
    product_id: number;
    product_name: string;
    sku: string;
    supplier_name: string;
    before: { purchase_price: number; currency: string } | null;
    after: { purchase_price: number; currency: string };
  }[];
}

export interface CostImportBatch {
  id: number;
  filename: string | null;
  rows_processed: number;
  rows_imported: number;
  rows_skipped: number;
  created_at: string;
  rolled_back_at: string | null;
  can_rollback: boolean;
}

export interface MatchSuggestionProduct {
  product_id: number;
  site: string;
  name: string;
  url: string;
  price: number | null;
  brand: string | null;
  pack_size: string | null;
  dosage: string | null;
  image_url: string | null;
  barcode: string | null;
}

export interface MatchSuggestion {
  match_id: number;
  canonical_name: string;
  confidence: number;
  needs_review: boolean;
  spread_pct: number | null;
  products: MatchSuggestionProduct[];
}

export interface RoiAction {
  type: string;
  severity: "info" | "opportunity" | "warning" | "critical";
  title: string;
  detail: string;
  product_name: string | null;
  product_url: string | null;
  current_value_azn: number | null;
  target_value_azn: number | null;
  /** Разница цены за ЕДИНИЦУ товара. + = профит при подъёме, − = потерянная маржа при опускании. */
  unit_gap_azn: number | null;
  /** % спред. + = клиент дешевле, − = конкурент дешевле. */
  spread_pct: number | null;
  /** @deprecated 2026-05-13 — всегда 0. Раньше = unit_gap × placeholder volume 30. */
  estimated_monthly_impact_azn: number;
  competitor_site: string | null;
}

export type AlertSite = "pharmonline" | "aptekonline" | "aloe";
export type AlertSort = "newest" | "oldest" | "site";

export interface AlertEvent {
  id: number;
  rule_type: string | null;
  severity: "info" | "warning" | "critical";
  title: string;
  detail: string | null;
  payload: Record<string, unknown> | null;
  destination_url: string | null;
  site: AlertSite | null;
  created_at: string;
  is_read?: boolean;
  read_at?: string | null;
  snoozed_until?: string | null;
}

export interface AlertPage {
  items: AlertEvent[];
  total: number;
  limit: number;
  offset: number;
  rule_types: string[];
}

export interface DataQuality {
  brand_extraction_rate_pct: number;
  products_with_good_brand: number;
  products_total: number;
  cross_3_count: number;
  cross_3_ceiling: number;
  cross_2_count: number;
  cross_2_pharm_apt_ceiling: number;
  cross_2_pending_suggestions: number;
  total_categories: number;
  manual_matches_last_7d: number;
  last_scrape_per_site: Record<string, string | null>;
}

export interface MatchQuality {
  total_matches: number;
  auto_matches: number;
  manual_matches: number;
  rejected_pairs: number;
  products_total: number;
  products_matched: number;
  coverage_pct: number;
  manual_pct: number;
}

export interface BrandShareRow {
  brand: string;
  counts: Record<string, number>;
  total: number;
  sites_with_brand: number;
  exclusive_to: string | null;
}

export interface PriceIndexRow {
  category: string | null;
  avg_client_price: number | null;
  avg_competitor_price: number | null;
  /** 100 = paritet, <100 клиент дешевле, >100 клиент дороже. */
  index: number | null;
  matched_skus: number | null;
}

export interface ForecastMover {
  product_id: number;
  site: string;
  name: string;
  n_points: number;
  first_price: number;
  last_price: number;
  change_pct: number;
  direction: "rising" | "falling" | "stable";
  forecast_7d_price: number | null;
  confidence: "low" | "medium" | "high";
}

/** Товар, найденный поиском, которого нет в строках сравнения (нет пары). */
export interface ComparisonOther {
  product_id: number;
  site: string;
  name: string;
  brand: string | null;
  url: string;
  /** null — цена ещё не собрана. */
  price: number | null;
  is_on_sale: boolean;
  country_code?: string | null;
  country_resolution_status?: "resolved" | "unknown" | "ambiguous" | "invalid";
  availability_status?: "in_stock" | "out_of_stock" | "unknown";
  age_days: number | null;
  stale: boolean;
}

export interface ComparisonSearchResult {
  rows: ComparisonRow[];
  others: ComparisonOther[];
  /** Всего найдено «прочих»; `others` может быть обрезан сервером. */
  others_total: number;
}

export interface ComparisonSuggestion {
  text: string;
  /** Сколько товаров каталога носят это имя. */
  count: number;
}

export interface CategoryComparisonRow {
  /** slug (Product.category) — ключ drill-down в /comparison?category=. */
  category: string;
  /** человекочитаемый ярлык (резолвлен по locale) или сырой slug-fallback. */
  label: string;
  matched_skus: number;
  avg_client_price: number;
  /** средняя цена каждого конкурента отдельно: {aptekonline, aloe}. */
  per_site_avg: Record<string, number>;
  avg_competitor_price: number;
  /** 100 = paritet, <100 клиент дешевле, >100 клиент дороже. */
  index: number;
  cheaper_count: number;
  pricier_count: number;
  parity_count: number;
  cheaper_pct: number;
  pricier_pct: number;
  parity_pct: number;
}

export interface CategoryComparisonCoverage {
  client_site: string;
  catalog_skus: number;
  categorized_skus: number;
  matched_skus: number;
  categorization_pct: number;
  matching_pct: number;
}

export interface CategoryCatalogRow {
  category: string;
  label: string;
  catalog_skus: number;
  comparable_skus: number;
  manual_skus: number;
}

export interface ManualCategoryOption {
  key: string;
  label: string;
}

export interface CategoryProductSuggestion {
  id: number;
  name: string;
  source_category: string | null;
  manual_category_key: string | null;
}

export interface SiteProduct {
  id: number;
  external_id: string;
  name: string;
  brand: string | null;
  category: string | null;
  url: string;
  image_url: string | null;
  price: number | null;
  discount_price: number | null;
  /** discount_price ?? price */
  effective_price: number | null;
  is_on_sale: boolean;
  last_seen_at: string | null;
}

export interface SiteProductsPage {
  items: SiteProduct[];
  total: number;
  limit: number;
  offset: number;
}

export interface SiteFacets {
  categories: { name: string; label?: string; count: number }[];
  brands: { name: string; count: number }[];
}

export interface SiteSummary {
  total_products: number;
  total_brands: number;
  exclusive_brands: number;
  on_sale_count: number;
  on_sale_pct: number;
  last_run_at: string | null;
  last_run_id: number | null;
}

export interface WatchlistLink {
  site: "pharmonline" | "aptekonline" | "aloe" | string;
  url: string;
  external_id: string | null;
  /** Backend statuses: pending, confirmed, missing */
  status: string;
}

export interface WatchlistItem {
  id: number;
  canonical_name: string;
  brand: string | null;
  dosage: string | null;
  pack_size: string | null;
  search_query: string | null;
  notes: string | null;
  is_active: boolean;
  links: WatchlistLink[];
}

export interface WatchlistCreatePayload {
  canonical_name: string;
  brand?: string;
  dosage?: string;
  pack_size?: string;
  search_query?: string;
  notes?: string;
  pharmonline_url?: string;
  aptekonline_url?: string;
  aloe_url?: string;
}

export interface WatchlistCategoryItem {
  id: number;
  category_id: number;
  key: string;
  label_ru: string;
  label_az: string | null;
  pharmonline_slug: string | null;
  aptekonline_slug: string | null;
  aloe_slug: string | null;
  notes: string | null;
  is_active: boolean;
  product_count: number;
  matched_product_count: number;
  comparison_count: number;
  missing_site_counts?: Record<string, number>;
}

export interface PriceHistoryPoint {
  date: string;
  price: number | null;
  is_on_sale: boolean;
}

export interface PriceHistoryResponse {
  product_id: number;
  site: string;
  name: string;
  days: number;
  points: PriceHistoryPoint[];
  delta_pct: number | null;
  current: number | null;
}

export interface NormalizeStats {
  products_total: number;
  products_normalized: number;
  products_needing_review: number;
  matches_needing_review: number;
  /** Legacy Match-row count; prefer the explicit fields above. */
  needs_review: number;
  coverage_pct: number;
  last_normalized_at: string | null;
  matches_by_strategy: Record<string, number>;
}

export interface RoiStatus {
  available: boolean;
  client_site: string;
  run_id: number | null;
  computed_at: string | null;
  run_started_at: string | null;
  run_finished_at: string | null;
  item_count: number;
  /** A change made since these were computed is waiting to be recalculated. */
  refresh_pending?: boolean;
}

export interface RoiRecommendations {
  items: RoiAction[];
  provenance: RoiStatus;
}

export interface AnchorProduct {
  product_id: number;
  site: string;
  name: string;
  brand: string | null;
  category: string | null;
  url: string;
  price: number | null;
}

export interface UnmatchedPair {
  match_id: number;
  canonical_name: string;
  canonical_brand: string | null;
  canonical_dosage: string | null;
  canonical_pack_size: string | null;
  anchor_products: AnchorProduct[];
}

export interface UnmatchedPairsPage {
  items: UnmatchedPair[];
  total: number;
  limit: number;
  offset: number;
}

export interface MatchUpdated {
  match_id: number;
  canonical_name: string;
  is_manual: boolean;
  match_strategy: string | null;
  products: { product_id: number; site: string; name: string; url: string }[];
}

export interface MatchAlternative {
  product_id: number;
  name: string;
  url: string | null;
  price: number | null;
  score: number;
}

export interface CandidateAnalog {
  product_id: number;
  site: string;
  name: string;
  brand: string | null;
  url: string | null;
  image_url: string | null;
  price: number | null;
  score: number;
  auto_safe: boolean;
}

export interface Recipient {
  id: number;
  email: string;
  name: string | null;
  role: "admin" | "viewer";
  is_active: boolean;
  daily_digest: boolean;
  weekly_digest: boolean;
  email_severity_min: "off" | "info" | "warning" | "critical" | null;
  telegram_chat_id: string | null;
  last_login_at: string | null;
  created_at: string | null;
}

export interface RecipientCreate {
  email: string;
  name?: string | null;
  role?: "admin" | "viewer";
  daily_digest?: boolean;
  weekly_digest?: boolean;
  email_severity_min?: "off" | "info" | "warning" | "critical" | null;
}

export interface RecipientUpdate {
  name?: string | null;
  role?: "admin" | "viewer";
  is_active?: boolean;
  daily_digest?: boolean;
  weekly_digest?: boolean;
  email_severity_min?: "off" | "info" | "warning" | "critical" | null;
}

export interface RunRow {
  id: number;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  products_scraped: number;
  products_per_site: Record<string, number> | null;
  run_quality: RunQuality | null;
  sites_completed: string | null;
  error_message: string | null;
}

export interface RunBreakdown {
  run_id: number;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  products_scraped: number;
  products_per_site: Record<string, number>;
  products_per_site_category: Record<string, Record<string, number>>;
  run_quality: RunQuality | null;
  sites_completed: string | null;
}

export interface RunHistoryPage {
  items: RunRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuditLogRow {
  id: number;
  actor_user_id: number | null;
  actor_email: string | null;
  action: string;
  resource: string;
  response_status: number;
  request_id: string | null;
  created_at: string;
}

export interface AuditLogPage {
  items: AuditLogRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface RunQualitySite {
  status: "ok" | "degraded" | "failed";
  products: number;
  items_expected: number;
  items_completed: number;
  items_failed: number;
  baseline_products: number | null;
  baseline_fraction: number | null;
  reasons: string[];
  errors: string[];
  errors_truncated: number;
  items_truncated: number;
}

export interface RunQuality {
  version: number;
  mode: string;
  baseline_enforced: boolean;
  full_catalog_verified: boolean;
  financially_eligible: boolean;
  sites: Record<string, RunQualitySite>;
}

export interface LatestRunBySite {
  site: string;
  run: RunRow | null;
  latest_attempt: RunRow | null;
}

export interface CategoryRow {
  id: number;
  key: string;
  label_ru: string;
  label_az: string | null;
  pharmonline_slug: string | null;
  aptekonline_slug: string | null;
  aloe_slug: string | null;
  is_active: boolean;
}

export interface CategoriesPage {
  items: CategoryRow[];
  total: number;
  limit: number;
  offset: number;
  stats: {
    total: number;
    active: number;
    cross2: number;
    cross3: number;
    pharmonline: number;
    aptekonline: number;
    aloe: number;
  };
}

export interface CategorySuggestion {
  site_a_slug: string;
  site_b_slug: string;
  shared_brands_count: number;
  sample_brands: string[];
  site_a_products: number;
  site_b_products: number;
  already_mapped: boolean;
}

export interface NotifPrefs {
  telegram_chat_id: string | null;
  email_severity_min: string | null;
  telegram_severity_min: string | null;
  quiet_hours: string | null;
  daily_digest: boolean;
  weekly_digest: boolean;
}

// ─── API calls ─────────────────────────────────────────────────────────────

export interface HealthSite {
  site: string;
  last_seen_at: string | null;
  hours_since: number | null;
  max_age_hours: number;
  /** How often this site is scheduled for a full scan. Optional: an older
   *  backend does not send it yet, so consumers must tolerate `undefined`. */
  cadence_hours?: number;
}

export interface Health {
  status: "up" | "degraded";
  last_run_at: string | null;
  last_run_status: string | null;
  db_ping_ms: number | null;
  redis_ping_ms: number | null;
  sites: HealthSite[];
  staleness_warning: boolean;
  full_catalog_run_at: string | null;
  full_catalog_status: string | null;
  full_catalog_verified: boolean;
  product_policy: {
    country_mode?: string;
    availability_mode?: string;
    full_catalog_trust_ready?: boolean;
    country_trust_ready?: boolean;
    availability_trust_ready?: boolean;
    policy_ready?: boolean;
    sites?: Array<Record<string, unknown>>;
  };
}

export interface SystemStatus extends Health {
  queue: {
    pending: number;
    running: number;
    oldest_pending_at: string | null;
  };
  proxy: {
    provider: "decodo";
    configured: boolean;
    sites: string[];
    pool_size: number;
  };
  digests: {
    daily: { enabled: boolean; schedule_baku: string };
    weekly: { enabled: boolean; schedule_baku: string };
  };
}

export const api = {
  // Auth
  authRequest: (email: string) =>
    request<{ sent: boolean; detail: string }>("/auth/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  logout: () => request<{ ok: true }>("/auth/logout", { method: "POST" }),

  // Health (public, no auth)
  health: () => request<Health>("/health"),
  systemStatus: () => request<SystemStatus>("/api/v1/dash/system-status"),

  // User
  me: () => request<MeOut>("/api/v1/dash/me"),

  // Data
  comparison: (
    params: {
      search?: string;
      min_sites?: number;
      site_filter?: string;
      limit?: number;
      category?: string;
    } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.search) q.set("search", params.search);
    if (params.min_sites != null) q.set("min_sites", String(params.min_sites));
    if (params.site_filter) q.set("site_filter", params.site_filter);
    if (params.limit) q.set("limit", String(params.limit));
    if (params.category) q.set("category", params.category);
    return request<ComparisonRow[]>(`/api/v1/dash/comparison?${q}`);
  },
  /** Поиск по всему каталогу: строки сравнения + товары без пары. */
  comparisonSearch: (params: {
    q: string;
    min_sites?: number;
    category?: string;
  }) => {
    const q = new URLSearchParams({ q: params.q });
    if (params.min_sites != null) q.set("min_sites", String(params.min_sites));
    if (params.category) q.set("category", params.category);
    return request<ComparisonSearchResult>(
      `/api/v1/dash/comparison/search?${q}`,
    );
  },
  comparisonSuggest: (q: string) =>
    request<ComparisonSuggestion[]>(
      `/api/v1/dash/comparison/suggest?q=${encodeURIComponent(q)}`,
    ),
  /** Excel текущей выборки; первым листом — товары с разной ценой. */
  comparisonExport: async (params: {
    search?: string;
    min_sites?: number;
    category?: string;
    with_aloe?: boolean;
    diff_only?: boolean;
    locale: string;
  }): Promise<{ blob: Blob; filename: string }> => {
    const q = new URLSearchParams({ locale: params.locale });
    if (params.search) q.set("search", params.search);
    if (params.min_sites != null) q.set("min_sites", String(params.min_sites));
    if (params.category) q.set("category", params.category);
    if (params.with_aloe) q.set("with_aloe", "true");
    if (params.diff_only) q.set("diff_only", "true");
    const res = await fetch(`${BASE}/api/v1/dash/comparison/export.xlsx?${q}`, {
      credentials: "include",
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new ApiError(res.status, text || res.statusText);
    }
    const named = /filename="([^"]+)"/.exec(
      res.headers.get("Content-Disposition") ?? "",
    );
    return {
      blob: await res.blob(),
      filename: named?.[1] ?? "comparison.xlsx",
    };
  },
  // Phase 4 — Pricing config + cost CSV upload
  pricingGet: () => request<PricingConfig>("/api/v1/dash/settings/pricing"),
  pricingUpdate: (cfg: PricingConfig) =>
    request<PricingConfig>("/api/v1/dash/settings/pricing", {
      method: "PUT",
      body: JSON.stringify(cfg),
    }),
  costsCsvImport: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${BASE}/api/v1/dash/settings/costs/import`, {
      method: "POST",
      credentials: "include",
      body: form,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new ApiError(res.status, text || res.statusText);
    }
    return res.json() as Promise<CostImportResult>;
  },
  costsCsvPreview: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${BASE}/api/v1/dash/settings/costs/preview`, {
      method: "POST",
      credentials: "include",
      body: form,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new ApiError(res.status, text || res.statusText);
    }
    return res.json() as Promise<CostImportPreview>;
  },
  costImportHistory: () =>
    request<CostImportBatch[]>("/api/v1/dash/settings/costs/imports"),
  costImportRollback: (batchId: number) =>
    request<{ ok: true; batch_id: number; rows_rolled_back: number }>(
      `/api/v1/dash/settings/costs/imports/${batchId}/rollback`,
      { method: "POST" },
    ),

  matchSuggestions: (
    params: {
      confidence_max?: number;
      only_needs_review?: boolean;
      limit?: number;
    } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.confidence_max != null)
      q.set("confidence_max", String(params.confidence_max));
    if (params.only_needs_review) q.set("only_needs_review", "true");
    if (params.limit != null) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<MatchSuggestion[]>(
      `/api/v1/dash/matches/suggestions${qs ? `?${qs}` : ""}`,
    );
  },
  matchConfirm: (matchId: number) =>
    request<void>(`/api/v1/dash/matches/${matchId}/confirm`, {
      method: "POST",
    }),
  matchReject: (matchId: number) =>
    request<void>(`/api/v1/dash/matches/${matchId}/reject`, { method: "POST" }),
  matchRelink: (matchId: number, site: string, url: string) =>
    request<{ ok: boolean; product_id: number; name: string; site: string }>(
      `/api/v1/dash/matches/${matchId}/relink`,
      { method: "POST", body: JSON.stringify({ site, url }) },
    ),
  matchAlternatives: (matchId: number, site: string, limit = 6) =>
    request<{ items: MatchAlternative[] }>(
      `/api/v1/dash/matches/${matchId}/alternatives?site=${encodeURIComponent(site)}&limit=${limit}`,
    ),
  roiActions: (client_site?: string, locale?: string) => {
    const q = new URLSearchParams();
    if (client_site) q.set("client_site", client_site);
    if (locale) q.set("locale", locale);
    const qs = q.toString();
    // ROI compute может быть тяжёлым (matcher join), ставим явно 15с timeout
    return request<RoiAction[]>(
      `/api/v1/dash/roi/actions${qs ? `?${qs}` : ""}`,
      { timeoutMs: 15_000 },
    );
  },
  roiStatus: (client_site?: string) => {
    const q = new URLSearchParams();
    if (client_site) q.set("client_site", client_site);
    const qs = q.toString();
    return request<RoiStatus>(`/api/v1/dash/roi/status${qs ? `?${qs}` : ""}`);
  },
  roiRecommendations: (client_site?: string, locale?: string) => {
    const q = new URLSearchParams();
    if (client_site) q.set("client_site", client_site);
    if (locale) q.set("locale", locale);
    const qs = q.toString();
    return request<RoiRecommendations>(
      `/api/v1/dash/roi/recommendations${qs ? `?${qs}` : ""}`,
      { timeoutMs: 15_000 },
    );
  },
  alerts: (
    params: {
      severity?: string;
      limit?: number;
      include_read?: boolean;
      include_snoozed?: boolean;
    } = {},
  ) => {
    const q = new URLSearchParams({ limit: String(params.limit ?? 100) });
    if (params.severity) q.set("severity", params.severity);
    if (params.include_read) q.set("include_read", "true");
    if (params.include_snoozed) q.set("include_snoozed", "true");
    return request<AlertEvent[]>(`/api/v1/dash/alerts?${q}`);
  },
  alertsPage: (
    params: {
      view?: "inbox" | "snoozed" | "read";
      severity?: string;
      rule_type?: string;
      site?: AlertSite | "general";
      sort?: AlertSort;
      hours?: number;
      limit?: number;
      offset?: number;
    } = {},
  ) => {
    const q = new URLSearchParams({
      view: params.view ?? "inbox",
      limit: String(params.limit ?? 50),
      offset: String(params.offset ?? 0),
      hours: String(params.hours ?? 168),
    });
    if (params.severity) q.set("severity", params.severity);
    if (params.rule_type) q.set("rule_type", params.rule_type);
    if (params.site) q.set("site", params.site);
    if (params.sort && params.sort !== "newest") q.set("sort", params.sort);
    return request<AlertPage>(`/api/v1/dash/alerts/page?${q}`);
  },
  alertsCounts: () =>
    request<{ unread: number; snoozed: number; read: number; total: number }>(
      "/api/v1/dash/alerts/counts",
    ),
  alertPatch: (
    id: number,
    payload: { is_read?: boolean; snooze_hours?: number },
  ) =>
    request<{ ok: true; id: number }>(`/api/v1/dash/alerts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  alertsMarkAllRead: () =>
    request<{ affected: number }>("/api/v1/dash/alerts/mark-all-read", {
      method: "POST",
    }),
  alertsBulk: (
    ids: number[],
    action:
      | "mark_read"
      | "mark_unread"
      | "snooze_24h"
      | "snooze_7d"
      | "snooze_clear",
  ) =>
    request<{ affected: number; action: string }>("/api/v1/dash/alerts/bulk", {
      method: "POST",
      body: JSON.stringify({ ids, action }),
    }),
  matchQuality: () => request<MatchQuality>("/api/v1/dash/match-quality"),
  dataQuality: () => request<DataQuality>("/api/v1/dash/data-quality"),
  normalizeStats: () => request<NormalizeStats>("/api/v1/dash/normalize/stats"),
  unmatchedPairs: (params: {
    site: string;
    category?: string;
    limit?: number;
    offset?: number;
  }) => {
    const q = new URLSearchParams({ site: params.site });
    if (params.category) q.set("category", params.category);
    if (params.limit != null) q.set("limit", String(params.limit));
    if (params.offset != null) q.set("offset", String(params.offset));
    return request<UnmatchedPairsPage>(`/api/v1/dash/unmatched-pairs?${q}`);
  },
  matchAddProduct: (match_id: number, product_id: number) =>
    request<MatchUpdated>(`/api/v1/dash/matches/${match_id}/add-product`, {
      method: "POST",
      body: JSON.stringify({ product_id }),
    }),
  matchCreateWithProducts: (product_ids: number[]) =>
    request<MatchUpdated>("/api/v1/dash/matches/create-with-products", {
      method: "POST",
      body: JSON.stringify({ product_ids }),
    }),
  matcherCounts: () =>
    request<Record<string, number>>("/api/v1/dash/matcher/counts"),
  matchCandidateAnalogs: (match_id: number, site: string, limit = 6) => {
    const q = new URLSearchParams({ site, limit: String(limit) });
    return request<{ items: CandidateAnalog[] }>(
      `/api/v1/dash/matches/${match_id}/candidate-analogs?${q}`,
    );
  },
  recipients: () => request<Recipient[]>("/api/v1/dash/recipients"),
  recipientCreate: (payload: RecipientCreate) =>
    request<Recipient>("/api/v1/dash/recipients", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  recipientUpdate: (id: number, payload: RecipientUpdate) =>
    request<Recipient>(`/api/v1/dash/recipients/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  recipientDelete: (id: number, hard = false) =>
    request<void>(`/api/v1/dash/recipients/${id}${hard ? "?hard=true" : ""}`, {
      method: "DELETE",
    }),
  recipientSendLoginLink: (id: number) =>
    request<{ ok: boolean; email: string }>(
      `/api/v1/dash/recipients/${id}/send-login-link`,
      { method: "POST" },
    ),
  brandShare: (params: { top_n?: number; site?: string } = {}) => {
    const q = new URLSearchParams();
    q.set("top_n", String(params.top_n ?? 30));
    if (params.site) q.set("site", params.site);
    return request<BrandShareRow[]>(`/api/v1/dash/brand-share?${q}`);
  },
  priceIndex: (client_site?: string) => {
    const q = new URLSearchParams();
    if (client_site) q.set("client_site", client_site);
    const qs = q.toString();
    return request<PriceIndexRow[]>(
      `/api/v1/dash/price-index${qs ? `?${qs}` : ""}`,
    );
  },
  forecastMovers: () =>
    request<ForecastMover[]>("/api/v1/dash/forecast/movers"),
  categoryComparison: (client_site?: string, locale?: string) => {
    const q = new URLSearchParams();
    if (client_site) q.set("client_site", client_site);
    if (locale) q.set("locale", locale);
    const qs = q.toString();
    return request<CategoryComparisonRow[]>(
      `/api/v1/dash/category-comparison${qs ? `?${qs}` : ""}`,
    );
  },
  categoryComparisonCoverage: (client_site = "pharmonline") =>
    request<CategoryComparisonCoverage>(
      `/api/v1/dash/category-comparison/coverage?client_site=${encodeURIComponent(client_site)}`,
    ),
  categoryCatalog: (client_site: string, locale: string) =>
    request<CategoryCatalogRow[]>(
      `/api/v1/dash/category-comparison/catalog?client_site=${encodeURIComponent(client_site)}&locale=${encodeURIComponent(locale)}`,
    ),
  manualCategoryOptions: (locale: string) =>
    request<ManualCategoryOption[]>(
      `/api/v1/dash/category-comparison/manual-categories?locale=${encodeURIComponent(locale)}`,
    ),
  categoryProductSuggestions: (q: string) =>
    request<CategoryProductSuggestion[]>(
      `/api/v1/dash/category-comparison/product-suggestions?q=${encodeURIComponent(q)}`,
    ),
  assignProductCategory: (productId: number, categoryKey: string | null) =>
    request<CategoryProductSuggestion>(
      `/api/v1/dash/category-comparison/products/${productId}/category`,
      {
        method: "PATCH",
        body: JSON.stringify({ category_key: categoryKey }),
      },
    ),
  siteProducts: (params: {
    site: string;
    category?: string;
    brand?: string;
    search?: string;
    on_sale?: boolean;
    limit?: number;
    offset?: number;
  }) => {
    const q = new URLSearchParams({ site: params.site });
    if (params.category) q.set("category", params.category);
    if (params.brand) q.set("brand", params.brand);
    if (params.search) q.set("search", params.search);
    if (params.on_sale != null) q.set("on_sale", String(params.on_sale));
    if (params.limit != null) q.set("limit", String(params.limit));
    if (params.offset != null) q.set("offset", String(params.offset));
    return request<SiteProductsPage>(`/api/v1/dash/products?${q}`);
  },
  siteProductsFacets: (site: string, locale = "ru") => {
    const q = new URLSearchParams({ site, locale });
    return request<SiteFacets>(`/api/v1/dash/products/facets?${q}`);
  },
  siteProductsSummary: (site: string) =>
    request<SiteSummary>(
      `/api/v1/dash/products/summary?site=${encodeURIComponent(site)}`,
    ),
  runs: (limit = 30) => request<RunRow[]>(`/api/v1/dash/runs?limit=${limit}`),
  runsHistory: (
    params: {
      limit?: number;
      offset?: number;
      status?: string;
      site?: string;
    } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.limit != null) q.set("limit", String(params.limit));
    if (params.offset != null) q.set("offset", String(params.offset));
    if (params.status) q.set("status", params.status);
    if (params.site) q.set("site", params.site);
    return request<RunHistoryPage>(`/api/v1/dash/runs/history?${q}`);
  },
  auditLog: (params: { limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.limit != null) q.set("limit", String(params.limit));
    if (params.offset != null) q.set("offset", String(params.offset));
    return request<AuditLogPage>(`/api/v1/dash/audit-log?${q}`);
  },
  runsLatestBySite: () =>
    request<LatestRunBySite[]>("/api/v1/dash/runs/latest-by-site"),
  runBreakdown: (id: number) =>
    request<RunBreakdown>(`/api/v1/dash/runs/${id}/breakdown`),
  categories: () => request<CategoryRow[]>("/api/v1/dash/categories"),
  categoriesPage: (
    params: {
      limit?: number;
      offset?: number;
      search?: string;
      site?: string;
      active_only?: boolean;
      coverage?: string;
    } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.limit != null) q.set("limit", String(params.limit));
    if (params.offset != null) q.set("offset", String(params.offset));
    if (params.search) q.set("search", params.search);
    if (params.site) q.set("site", params.site);
    if (params.active_only) q.set("active_only", "true");
    if (params.coverage) q.set("coverage", params.coverage);
    return request<CategoriesPage>(`/api/v1/dash/categories/page?${q}`);
  },
  categoryCreate: (payload: Omit<CategoryRow, "id">) =>
    request<CategoryRow>("/api/v1/dash/categories", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  categoryUpdate: (id: number, payload: Omit<CategoryRow, "id">) =>
    request<{ ok: true }>(`/api/v1/dash/categories/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  categoryDelete: (id: number) =>
    request<void>(`/api/v1/dash/categories/${id}`, { method: "DELETE" }),
  categorySuggestions: (params: {
    site_a: string;
    site_b: string;
    min_overlap?: number;
    limit?: number;
  }) => {
    const q = new URLSearchParams({
      site_a: params.site_a,
      site_b: params.site_b,
    });
    if (params.min_overlap != null)
      q.set("min_overlap", String(params.min_overlap));
    if (params.limit != null) q.set("limit", String(params.limit));
    return request<CategorySuggestion[]>(
      `/api/v1/dash/categories/suggestions?${q}`,
    );
  },
  categoryMappingCreate: (payload: {
    site_a: string;
    site_a_slug: string;
    site_b: string;
    site_b_slug: string;
    label_ru?: string | null;
    label_az?: string | null;
  }) =>
    request<{ id: number; action: "created" | "extended"; key: string }>(
      "/api/v1/dash/categories/mapping",
      { method: "POST", body: JSON.stringify(payload) },
    ),
  rejectMatch: (id: number) =>
    request<void>(`/api/v1/dash/matches/${id}/reject`, { method: "POST" }),

  // Notification preferences
  notifPrefs: () => request<NotifPrefs>("/api/v1/dash/me/notifications"),
  notifPrefsUpdate: (patch: Partial<NotifPrefs>) =>
    request<{ ok: true }>("/api/v1/dash/me/notifications", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  notifUnbindTelegram: () =>
    request<void>("/api/v1/dash/me/notifications/telegram", {
      method: "DELETE",
    }),
  integrations: () => request<IntegrationsStatus>("/api/v1/dash/integrations"),
  changePassword: (current_password: string, new_password: string) =>
    request<{ ok: true }>("/api/v1/dash/me/password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),
  scrapeTrigger: (payload: {
    mode: "all" | "category";
    category_id?: number;
  }) =>
    request<{ id: number; status: string }>("/api/v1/dash/scrape/trigger", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  digestSendTest: (kind: "daily" | "weekly" = "daily") =>
    request<{ ok: boolean; recipients_sent: number }>(
      `/api/v1/dash/digest/send-test?kind=${kind}`,
      { method: "POST", timeoutMs: 30_000 },
    ),
  watchlistList: () => request<WatchlistItem[]>("/api/v1/dash/watchlist"),
  watchlistCreate: (payload: WatchlistCreatePayload) =>
    request<{ id: number }>("/api/v1/dash/watchlist", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  watchlistDelete: (id: number) =>
    request<void>(`/api/v1/dash/watchlist/${id}`, { method: "DELETE" }),
  watchlistCategoriesList: () =>
    request<WatchlistCategoryItem[]>("/api/v1/dash/watchlist/categories"),
  watchlistCategoryCreate: (payload: { category_id: number; notes?: string }) =>
    request<{ id: number }>("/api/v1/dash/watchlist/categories", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  watchlistCategoryDelete: (id: number) =>
    request<void>(`/api/v1/dash/watchlist/categories/${id}`, {
      method: "DELETE",
    }),
  productPriceHistory: (product_id: number, days = 30) =>
    request<PriceHistoryResponse>(
      `/api/v1/dash/products/${product_id}/price-history?days=${days}`,
    ),
  /**
   * Batch версия — за один HTTP-запрос история цен для нескольких товаров.
   * Используется на /comparison TrendPanel (раньше делал N×3 fetch).
   * Backend лимит 50 ids/запрос. Возвращает dict keyed by stringified id;
   * продукты без доступа или не найденные просто не появятся в ответе.
   */
  productPriceHistoryBatch: (product_ids: number[], days = 30) => {
    if (product_ids.length === 0) {
      return Promise.resolve<Record<string, PriceHistoryResponse>>({});
    }
    const ids = product_ids.join(",");
    return request<Record<string, PriceHistoryResponse>>(
      `/api/v1/dash/products/price-history?ids=${encodeURIComponent(ids)}&days=${days}`,
    );
  },
  scrapeRequests: (limit = 10) =>
    request<ScrapeRequestRow[]>(`/api/v1/dash/scrape/requests?limit=${limit}`),
  scrapeRequestsHistory: (
    params: { limit?: number; offset?: number; status?: string } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.limit != null) q.set("limit", String(params.limit));
    if (params.offset != null) q.set("offset", String(params.offset));
    if (params.status) q.set("status", params.status);
    return request<ScrapeRequestHistoryPage>(
      `/api/v1/dash/scrape/requests/history?${q}`,
    );
  },
};

export interface ScrapeRequestRow {
  id: number;
  mode: string;
  category_id: number | null;
  sites: string | null;
  status: "pending" | "running" | "ok" | "degraded" | "failed";
  requested_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  run_id: number | null;
  error_message: string | null;
  /** Total products across all sites in the terminal run. */
  products_scraped: number | null;
  /** Per-site breakdown {site: count} from a terminal run. */
  products_per_site: Record<string, number> | null;
}

export interface ScrapeRequestHistoryPage {
  items: ScrapeRequestRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface IntegrationsStatus {
  smtp: boolean;
  smtp_from: string | null;
  telegram: boolean;
  telegram_bot_username: string | null;
  sentry: boolean;
  scraperapi: boolean;
  scraperapi_sites: string[];
  decodo: boolean;
  decodo_sites: string[];
  decodo_pool_size: number;
}
