# Retired Mac Scrape Runtime

Production scraping is fully server-side as of 2026-07-03. The paid services own
the scrape path:

| Site | Runtime |
|---|---|
| pharmonline.az | prod server, see CLAUDE.md "Расписание сбора" |
| aptekonline.az | prod server, Decodo AZ residential proxy |
| aloe.az | prod server, direct RSC/HTTP parser |

The production server moved from Hetzner to Contabo on 2026-09-03 and the
Hetzner host was deleted. Nothing in this directory defaults to the old address
any more: the retired scripts have no default target at all, and every SSH
connection they open themselves accepts only the host key pinned in
`infra/prod_known_hosts`. One gap remains in the retired pair: both use whatever
already listens on `localhost:5433` instead of opening their own tunnel, so
make sure nothing else holds that port before a DR run.

`com.pharmacy-monitor.scrape` and `com.pharmacy-monitor.watch` must stay
unloaded/disabled. The local scrape scripts are fail-closed and exit without
scraping unless `PHARMACY_MONITOR_ENABLE_MAC_SCRAPE=1` is set for an explicit
one-off disaster-recovery run.

`com.pharmacy-monitor.db-tunnel` (a diagnostics/Postgres tunnel, not a scraper)
is stale too: it pointed at the deleted Hetzner host. Unload it on the Mac
(`launchctl bootout gui/$UID/com.pharmacy-monitor.db-tunnel`); the copy here
carries a placeholder instead of an address until someone decides the tunnel is
still wanted.

## Files

- `run-scrape.sh` - retired scraper wrapper; exits unless explicitly enabled.
- `watch-scrape-queue.sh` - retired UI queue watcher; exits unless explicitly enabled.
- `com.pharmacy-monitor.scrape.plist` - retired launchd unit; keep unloaded.
- `com.pharmacy-monitor.watch.plist` - retired launchd unit; keep unloaded.
- `com.pharmacy-monitor.db-tunnel.plist` - stale diagnostics DB tunnel; placeholder target, keep unloaded.
- `fetch-backup.sh` - pulls the newest encrypted DB dump off the production
  server. The only way a copy leaves the server today; runs from any machine
  whose SSH key the server accepts, not just the Mac.

## Emergency DR Only

Do not use Mac scraping as a normal fallback. First check the server-side paid
services, proxy balances, and systemd timers. If an explicit one-off DR run is
approved, run the script manually with:

```bash
PHARMACY_MONITOR_ENABLE_MAC_SCRAPE=1 PROD_HOST=13.140.186.143 bash infra/local/run-scrape.sh --site <site> --mode category --no-alerts
```

`PROD_HOST` must be the IP exactly as written: the pinned key is filed under it,
so a hostname is refused. Errors go to the script's log file, not the terminal.
This path has not been run against the Contabo server: the Mac's SSH key may not
be authorised there at all.

After the DR run, confirm the Mac launchd scrape/watch units are still disabled.
