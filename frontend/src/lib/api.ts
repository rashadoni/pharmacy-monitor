/**
 * API client. All requests are same-origin in prod (Caddy proxies /api → FastAPI).
 * In dev, Next.js rewrites /api/* to localhost:8080 (see next.config.mjs).
 *
 * Auth: JWT cookie (httpOnly, set by /auth/verify endpoint). No manual token handling.
 */
const BASE = ""; // same origin

async function request<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  const timeoutMs = init?.timeoutMs ?? 30_000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}${path}`, {
      credentials: "include",
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      signal: controller.signal,
      ...init,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new ApiError(res.status, text || res.statusText);
    }
    if (res.status === 204) return undefined as T;
    return res.json();
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(
        408,
        `Запрос дольше ${Math.round(timeoutMs / 1000)}с — сервер не ответил`,
      );
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
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

export const api = {
  // Auth
  authRequest: (email: string) =>
    request<{ sent: boolean; detail: string }>("/auth/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  logout: () => request<{ ok: true }>("/auth/logout", { method: "POST" }),

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
  roiActions: (client_site?: string) => {
    const q = new URLSearchParams();
    if (client_site) q.set("client_site", client_site);
    const qs = q.toString();
    // ROI compute может быть тяжёлым (matcher join), ставим явно 15с timeout
    return request<RoiAction[]>(
      `/api/v1/dash/roi/actions${qs ? `?${qs}` : ""}`,
      { timeoutMs: 15_000 },
    );
  },
  alerts: (severity?: string, limit = 100) => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (severity) q.set("severity", severity);
    return request<AlertEvent[]>(`/api/v1/dash/alerts?${q}`);
  },
  matchQuality: () => request<MatchQuality>("/api/v1/dash/match-quality"),
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
