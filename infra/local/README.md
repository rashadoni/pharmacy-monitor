# Retired Mac Scrape Runtime

Production scraping is fully server-side as of 2026-07-03. The paid services own
the scrape path:

| Site | Runtime |
|---|---|
| pharmonline.az | Hetzner prod, DDP through paid proxy |
| aptekonline.az | Hetzner prod, Decodo AZ residential proxy |
| aloe.az | Hetzner prod, direct RSC/HTTP parser |

`com.pharmacy-monitor.scrape` and `com.pharmacy-monitor.watch` must stay
unloaded/disabled. The local scrape scripts are fail-closed and exit without
scraping unless `PHARMACY_MONITOR_ENABLE_MAC_SCRAPE=1` is set for an explicit
one-off disaster-recovery run.

The only local launchd job that may remain active is
`com.pharmacy-monitor.db-tunnel`, which is a diagnostics/Postgres tunnel and not
a scraper.

## Files

- `run-scrape.sh` - retired scraper wrapper; exits unless explicitly enabled.
- `watch-scrape-queue.sh` - retired UI queue watcher; exits unless explicitly enabled.
- `com.pharmacy-monitor.scrape.plist` - retired launchd unit; keep unloaded.
- `com.pharmacy-monitor.watch.plist` - retired launchd unit; keep unloaded.
- `com.pharmacy-monitor.db-tunnel.plist` - diagnostics DB tunnel; not a scraper.
- `fetch-backup.sh` - local backup fetch helper; not a scraper.

## Emergency DR Only

Do not use Mac scraping as a normal fallback. First check the server-side paid
services, proxy balances, and systemd timers. If an explicit one-off DR run is
approved, run the script manually with:

```bash
PHARMACY_MONITOR_ENABLE_MAC_SCRAPE=1 bash infra/local/run-scrape.sh --site <site> --mode category --no-alerts
```

After the DR run, confirm the Mac launchd scrape/watch units are still disabled.
