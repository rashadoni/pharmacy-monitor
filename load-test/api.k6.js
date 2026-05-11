/**
 * k6 load test for Pharmacy Monitor API.
 *
 * Goals:
 *   - Verify API stays under p95 < 500ms with 100 concurrent users
 *   - Find regressions before go-live
 *   - Catch DB / Redis bottlenecks early
 *
 * Pre-requisites:
 *   1. API running (uvicorn or systemd) on $BASE_URL
 *   2. JWT cookie obtained via magic-link flow:
 *        TOKEN=$(./.venv/bin/python -c "from src.tenants import issue_magic_token; ...")
 *        curl http://localhost:8080/auth/verify?token=$TOKEN -c cookies.txt
 *      Then export PHARMACY_JWT=$(grep pm_session cookies.txt | awk '{print $7}')
 *
 * Run:
 *   BASE_URL=https://your-domain.com PHARMACY_JWT="..." k6 run api.k6.js
 *
 * Stages:
 *   - 30s ramp 0 → 50 vus
 *   - 60s steady at 50 vus
 *   - 60s ramp 50 → 100 vus
 *   - 60s steady at 100 vus
 *   - 30s ramp down to 0
 *
 * Thresholds (test FAILS if violated):
 *   - p95 latency < 500ms across all endpoints
 *   - <1% error rate (4xx + 5xx)
 *   - dashboard endpoints p95 < 1000ms (heavier queries)
 */
import http from "k6/http";
import { check, sleep, group } from "k6";
import { Rate, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const JWT = __ENV.PHARMACY_JWT || "";
const API_KEY = __ENV.PHARMACY_API_KEY || "";

const errorRate = new Rate("errors");
const dashLatency = new Trend("dash_latency_ms");
const erpLatency = new Trend("erp_latency_ms");

export const options = {
  stages: [
    { duration: "30s", target: 50 },
    { duration: "60s", target: 50 },
    { duration: "60s", target: 100 },
    { duration: "60s", target: 100 },
    { duration: "30s", target: 0 },
  ],
  thresholds: {
    http_req_duration: ["p(95)<500"],
    "http_req_duration{group:::dashboard}": ["p(95)<1000"],
    errors: ["rate<0.01"],
    "checks": ["rate>0.99"],
  },
};

const cookieJar = http.cookieJar();
if (JWT) {
  cookieJar.set(BASE_URL, "pm_session", JWT);
}

export default function () {
  group("public", function () {
    const r = http.get(`${BASE_URL}/health`);
    check(r, { "health 200": (res) => res.status === 200 });
    if (r.status !== 200) errorRate.add(1);
  });

  if (!JWT) {
    sleep(1);
    return;
  }

  group("dashboard", function () {
    const endpoints = [
      "/api/v1/dash/me",
      "/api/v1/dash/comparison?min_sites=2",
      "/api/v1/dash/comparison?min_sites=2&search=nestle",
      "/api/v1/dash/roi/actions",
      "/api/v1/dash/alerts",
      "/api/v1/dash/match-quality",
      "/api/v1/dash/brand-share?top_n=15",
      "/api/v1/dash/runs",
      "/api/v1/dash/categories",
      "/api/v1/dash/me/notifications",
    ];
    for (const path of endpoints) {
      const r = http.get(`${BASE_URL}${path}`, {
        tags: { group: "dashboard" },
      });
      const ok = check(r, {
        [`${path} 200/304`]: (res) => res.status === 200 || res.status === 304,
      });
      if (!ok) errorRate.add(1);
      dashLatency.add(r.timings.duration);
      sleep(0.05);
    }
  });

  if (API_KEY) {
    group("erp_legacy", function () {
      const headers = { "X-API-Key": API_KEY };
      for (const path of [
        "/api/v1/products?limit=50",
        "/api/v1/comparisons",
        "/api/v1/alerts/recent?limit=20",
        "/api/v1/margin",
      ]) {
        const r = http.get(`${BASE_URL}${path}`, {
          headers,
          tags: { group: "erp" },
        });
        check(r, { [`${path} 200`]: (res) => res.status === 200 });
        if (r.status !== 200) errorRate.add(1);
        erpLatency.add(r.timings.duration);
        sleep(0.05);
      }
    });
  }

  sleep(1);
}

export function handleSummary(data) {
  return {
    "summary.json": JSON.stringify(data, null, 2),
    stdout: textSummary(data),
  };
}

function textSummary(data) {
  const m = data.metrics;
  const lines = [
    "",
    "═══ Pharmacy Monitor — Load Test Summary ═══",
    "",
    `  HTTP requests:        ${m.http_reqs.values.count}`,
    `  Failed requests:      ${(m.errors?.values.rate * 100 || 0).toFixed(2)}%`,
    `  Latency p50:          ${m.http_req_duration.values["p(50)"].toFixed(1)}ms`,
    `  Latency p95:          ${m.http_req_duration.values["p(95)"].toFixed(1)}ms`,
    `  Latency p99:          ${m.http_req_duration.values["p(99)"].toFixed(1)}ms`,
    `  Latency max:          ${m.http_req_duration.values.max.toFixed(1)}ms`,
    "",
    "  Threshold checks:",
    ...Object.entries(data.metrics)
      .filter(([_, v]) => v.thresholds)
      .map(([name, v]) => {
        const tags = Object.entries(v.thresholds)
          .map(([t, ok]) => `    ${ok.ok ? "✓" : "✗"} ${name}: ${t}`)
          .join("\n");
        return tags;
      }),
    "",
  ];
  return lines.join("\n");
}
