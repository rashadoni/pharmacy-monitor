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
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
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

/**
 * Преобразовать ApiError / Error в человекочитаемое сообщение для UI.
 * Анализирует Pydantic 422 (validation), 401/403/404/409 и timeout 408.
 */
export function friendlyError(err: unknown): string {
  if (err instanceof ApiError) {
    // Pydantic 422 detail обычно JSON: [{loc, msg, type}, ...]
    if (err.status === 422 && err.detail.startsWith("{")) {
      try {
        const data = JSON.parse(err.detail);
        if (Array.isArray(data?.detail)) {
          const first = data.detail[0];
          if (first?.msg) {
            const field = first.loc?.slice(-1)?.[0] ?? "поле";
            return `${field}: ${translatePydantic(first.msg)}`;
          }
        }
      } catch {
        /* fall through */
      }
    }
    if (err.status === 401) return "Нужно войти заново";
    if (err.status === 403) return "Нет прав доступа (только админы)";
    if (err.status === 404) return "Не найдено";
    if (err.status === 408) return err.detail;
    if (err.status === 409) {
      // 409 detail обычно уже на русском от backend
      try {
        const d = JSON.parse(err.detail);
        return d.detail ?? err.detail;
      } catch {
        return err.detail;
      }
    }
    if (err.status >= 500) return "Сервер недоступен. Попробуйте через минуту.";
    return err.detail || `Ошибка ${err.status}`;
  }
  if (err instanceof Error) return err.message;
  return "Неизвестная ошибка";
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

function translatePydantic(msg: string): string {
  const map: Record<string, string> = {
    "Field required": "обязательное поле",
    "Input should be a valid email address": "введите корректный email",
    "String should have at least 3 characters": "минимум 3 символа",
    "value is not a valid integer": "должно быть числом",
  };
  return map[msg] ?? msg;
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
  prices: Record<string, { price: number; is_on_sale: boolean; url: string; product_id: number }>;
  confidence: number;
  needs_review: boolean;
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
  rows_processed: number;
  rows_imported: number;
  rows_skipped: number;
  errors: string[];
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

export interface AlertEvent {
  id: number;
  rule_type: string | null;
  severity: "info" | "warning" | "critical";
  title: string;
  detail: string | null;
  payload: Record<string, unknown> | null;
  created_at: string;
  is_read?: boolean;
  read_at?: string | null;
  snoozed_until?: string | null;
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
  needs_review: number;
  coverage_pct: number;
  last_normalized_at: string | null;
  matches_by_strategy: Record<string, number>;
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
  sites_completed: string | null;
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
}

export interface Health {
  status: "up" | "degraded";
  last_run_at: string | null;
  last_run_status: string | null;
  db_ping_ms: number | null;
  redis_ping_ms: number | null;
  sites: HealthSite[];
  staleness_warning: boolean;
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

  // User
  me: () => request<MeOut>("/api/v1/dash/me"),

  // Data
  comparison: (params: { search?: string; min_sites?: number; site_filter?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.search) q.set("search", params.search);
    if (params.min_sites != null) q.set("min_sites", String(params.min_sites));
    if (params.site_filter) q.set("site_filter", params.site_filter);
    if (params.limit) q.set("limit", String(params.limit));
    return request<ComparisonRow[]>(`/api/v1/dash/comparison?${q}`);
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

  matchSuggestions: (params: {
    confidence_max?: number;
    only_needs_review?: boolean;
    limit?: number;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.confidence_max != null) q.set("confidence_max", String(params.confidence_max));
    if (params.only_needs_review) q.set("only_needs_review", "true");
    if (params.limit != null) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<MatchSuggestion[]>(
      `/api/v1/dash/matches/suggestions${qs ? `?${qs}` : ""}`,
    );
  },
  matchConfirm: (matchId: number) =>
    request<void>(`/api/v1/dash/matches/${matchId}/confirm`, { method: "POST" }),
  matchReject: (matchId: number) =>
    request<void>(`/api/v1/dash/matches/${matchId}/reject`, { method: "POST" }),
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
  alerts: (params: {
    severity?: string;
    limit?: number;
    include_read?: boolean;
    include_snoozed?: boolean;
  } = {}) => {
    const q = new URLSearchParams({ limit: String(params.limit ?? 100) });
    if (params.severity) q.set("severity", params.severity);
    if (params.include_read) q.set("include_read", "true");
    if (params.include_snoozed) q.set("include_snoozed", "true");
    return request<AlertEvent[]>(`/api/v1/dash/alerts?${q}`);
  },
  alertsCounts: () =>
    request<{ unread: number; snoozed: number; read: number; total: number }>(
      "/api/v1/dash/alerts/counts",
    ),
  alertPatch: (id: number, payload: { is_read?: boolean; snooze_hours?: number }) =>
    request<{ ok: true; id: number }>(`/api/v1/dash/alerts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  alertsMarkAllRead: () =>
    request<{ affected: number }>("/api/v1/dash/alerts/mark-all-read", { method: "POST" }),
  alertsBulk: (ids: number[], action:
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
  recipientDelete: (id: number) =>
    request<void>(`/api/v1/dash/recipients/${id}`, { method: "DELETE" }),
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
    return request<PriceIndexRow[]>(`/api/v1/dash/price-index${qs ? `?${qs}` : ""}`);
  },
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
  siteProductsFacets: (site: string) =>
    request<SiteFacets>(`/api/v1/dash/products/facets?site=${encodeURIComponent(site)}`),
  siteProductsSummary: (site: string) =>
    request<SiteSummary>(`/api/v1/dash/products/summary?site=${encodeURIComponent(site)}`),
  runs: (limit = 30) => request<RunRow[]>(`/api/v1/dash/runs?limit=${limit}`),
  runBreakdown: (id: number) =>
    request<RunBreakdown>(`/api/v1/dash/runs/${id}/breakdown`),
  categories: () => request<CategoryRow[]>("/api/v1/dash/categories"),
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
    const q = new URLSearchParams({ site_a: params.site_a, site_b: params.site_b });
    if (params.min_overlap != null) q.set("min_overlap", String(params.min_overlap));
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
    request<void>("/api/v1/dash/me/notifications/telegram", { method: "DELETE" }),
  integrations: () =>
    request<IntegrationsStatus>("/api/v1/dash/integrations"),
  changePassword: (current_password: string, new_password: string) =>
    request<{ ok: true }>("/api/v1/dash/me/password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),
  scrapeTrigger: (payload: { mode: "all" | "category"; category_id?: number }) =>
    request<{ id: number; status: string }>("/api/v1/dash/scrape/trigger", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  digestSendTest: (kind: "daily" | "weekly" = "daily") =>
    request<{ ok: boolean; recipients_sent: number }>(
      `/api/v1/dash/digest/send-test?kind=${kind}`,
      { method: "POST", timeoutMs: 30_000 },
    ),
  productPriceHistory: (product_id: number, days = 30) =>
    request<PriceHistoryResponse>(
      `/api/v1/dash/products/${product_id}/price-history?days=${days}`,
    ),
  scrapeRequests: (limit = 10) =>
    request<ScrapeRequestRow[]>(`/api/v1/dash/scrape/requests?limit=${limit}`),
};

export interface ScrapeRequestRow {
  id: number;
  mode: string;
  category_id: number | null;
  sites: string | null;
  status: "pending" | "running" | "ok" | "failed";
  requested_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  run_id: number | null;
  error_message: string | null;
  /** Total products across all sites in the run (filled when status=ok). */
  products_scraped: number | null;
  /** Per-site breakdown {site: count} from runs.products_per_site (filled when status=ok). */
  products_per_site: Record<string, number> | null;
}

export interface IntegrationsStatus {
  smtp: boolean;
  smtp_from: string | null;
  telegram: boolean;
  telegram_bot_username: string | null;
  sentry: boolean;
  scraperapi: boolean;
  scraperapi_sites: string[];
}
