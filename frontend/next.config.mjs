import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./i18n.ts");

/** @type {import('next').NextConfig} */
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  output: "standalone", // self-contained build for systemd deployment
  // In dev: proxy /api/* to FastAPI on :8080
  // In prod: Caddy handles routing, this is a no-op
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_URL}/api/:path*`,
      },
      {
        source: "/auth/:path*",
        destination: `${API_URL}/auth/:path*`,
      },
    ];
  },
  // Phase 6.1 retry (2026-05-28) — backward compat redirects.
  //
  // Framework-level `redirects()` (НЕ middleware!) — обходит next-intl
  // issue #524 standalone recursion. Сервер вернёт HTTP 307 ДО page render.
  //
  // Default locale = ru. Все legacy unprefixed URLs → /ru/<same-path>.
  async redirects() {
    const LEGACY_ROUTES = [
      "alerts",
      "analytics",
      "categories",
      "category-comparison",
      "comparison",
      "login",
      "matcher",
      "matches/review",
      "overview",
      "settings",
      "settings/pricing",
      "settings/users",
      "watchlist",
    ];
    return [
      // 1:1 route redirects (legacy /comparison → /ru/comparison)
      ...LEGACY_ROUTES.map((route) => ({
        source: `/${route}`,
        destination: `/ru/${route}`,
        permanent: false,
      })),
      // Dynamic: /site/[site] → /ru/site/[site]
      {
        source: "/site/:site",
        destination: "/ru/site/:site",
        permanent: false,
      },
    ];
  },
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "pharmonline.az" },
      { protocol: "https", hostname: "www.pharmonline.az" },
      { protocol: "https", hostname: "aptekonline.az" },
      { protocol: "https", hostname: "www.aptekonline.az" },
      { protocol: "https", hostname: "aloe.az" },
    ],
  },
};

export default withNextIntl(nextConfig);
