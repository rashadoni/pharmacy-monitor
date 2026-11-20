"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ExternalLink, ShoppingBag } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { QueryErrorState } from "@/components/query-error-state";
import {
  api,
  type WoltComparison,
  type WoltOfferPrice,
  type WoltRow,
  type WoltVenue,
} from "@/lib/api";
import { cn, formatPrice, formatRelative } from "@/lib/utils";

// Строк бывает ~1 400 — рисуем частями.
const PAGE = 100;

export default function WoltPage() {
  const t = useTranslations("wolt");
  const locale = useLocale();
  const [search, setSearch] = useState("");
  const [onlyDiff, setOnlyDiff] = useState(true);
  const [limit, setLimit] = useState(PAGE);
  const [debouncedSearch, setDebouncedSearch] = useState("");
  // Запрос — после паузы в наборе, а не на каждую клавишу.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const q = useQuery({
    queryKey: ["wolt-comparison", debouncedSearch, onlyDiff],
    queryFn: () => api.woltComparison(debouncedSearch, onlyDiff),
    placeholderData: keepPreviousData,
  });
  // Пока идёт новый запрос, на экране — ответ на прошлый: «ничего не найдено»
  // относилось бы не к тому, что набрано сейчас.
  const stale = q.isPlaceholderData || debouncedSearch !== search.trim();
  const data = q.data;
  const venues = data?.venues ?? [];
  const rows = data?.rows ?? [];
  const visible = rows.slice(0, limit);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <ShoppingBag className="h-6 w-6" aria-hidden="true" /> {t("title")}
        </h1>
        {data && (
          <p className="mt-1 text-sm text-muted-foreground">
            {t("schedule", { time: data.daily_sync_baku })}
          </p>
        )}
      </header>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {venues.map((venue) => (
          <VenueCard key={venue.venue} venue={venue} locale={locale} />
        ))}
      </div>

      {data && <Summary data={data} />}

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <input
          type="search"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setLimit(PAGE);
          }}
          placeholder={t("search")}
          aria-label={t("search")}
          maxLength={200}
          className="min-h-11 w-full rounded-md border border-border bg-background px-3 text-sm sm:max-w-sm md:min-h-9"
        />
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm md:min-h-9">
          <input
            type="checkbox"
            checked={onlyDiff}
            onChange={(event) => {
              setOnlyDiff(event.target.checked);
              setLimit(PAGE);
            }}
            className="h-4 w-4"
          />
          {data
            ? t("only_diff_count", { differ: data.totals.differ, rows: data.totals.rows })
            : t("only_diff")}
        </label>
      </div>

      {q.isPending && <p className="text-sm text-muted-foreground" role="status">{t("loading")}</p>}
      {q.isError && (
        <QueryErrorState message={t("error")} retryLabel={t("retry")} onRetry={() => void q.refetch()} />
      )}
      {stale && data && <p className="text-xs text-muted-foreground" role="status">{t("updating")}</p>}
      {data && !stale && rows.length === 0 && <p className="text-sm text-muted-foreground">{t("empty")}</p>}

      {rows.length > 0 && (
        <div className={cn("space-y-3", stale && "opacity-60")}>
          {/* Таблица — от md, на телефоне — карточки. */}
          <div className="hidden overflow-x-auto rounded-md border border-border md:block">
            <table className="w-full text-sm">
              <thead className="bg-secondary/60 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">{t("product")}</th>
                  {venues.map((venue) => (
                    <th
                      key={venue.venue}
                      className={cn("px-3 py-2 text-right font-medium", venue.is_owner && "bg-primary/10 text-foreground")}
                    >
                      {venue.title}
                    </th>
                  ))}
                  <th className="px-3 py-2 text-right font-medium">{t("spread")}</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((row) => (
                  <tr key={rowKey(row)} className="border-t border-border align-top">
                    <td className="px-3 py-2">{row.name}</td>
                    {venues.map((venue) => (
                      <td
                        key={venue.venue}
                        className={cn("px-3 py-2 text-right", venue.is_owner && "bg-primary/5")}
                      >
                        <PriceCell
                          offer={row.prices[venue.venue]}
                          venueTitle={venue.title}
                          cheapest={row.cheapest.includes(venue.venue)}
                          locale={locale}
                        />
                      </td>
                    ))}
                    <td className="px-3 py-2 text-right tabular-nums">
                      {row.spread_pct > 0 ? `${row.spread_pct.toFixed(1)}%` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <ul className="space-y-3 md:hidden">
            {visible.map((row) => (
              <li key={rowKey(row)} className="rounded-md border border-border p-3">
                <div className="flex items-start justify-between gap-3">
                  <span className="text-sm font-medium">{row.name}</span>
                  {row.spread_pct > 0 && (
                    <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                      {`${row.spread_pct.toFixed(1)}%`}
                    </span>
                  )}
                </div>
                <dl className="mt-2 space-y-1">
                  {venues.map((venue) => (
                    <div
                      key={venue.venue}
                      className={cn(
                        "flex items-center justify-between gap-3 text-sm",
                        venue.is_owner && "-mx-1 rounded bg-primary/5 px-1 font-medium",
                      )}
                    >
                      <dt className="text-muted-foreground">{venue.title}</dt>
                      <dd>
                        <PriceCell
                          offer={row.prices[venue.venue]}
                          venueTitle={venue.title}
                          cheapest={row.cheapest.includes(venue.venue)}
                          locale={locale}
                        />
                      </dd>
                    </div>
                  ))}
                </dl>
              </li>
            ))}
          </ul>

          <div className="flex flex-col items-start gap-2 text-sm text-muted-foreground sm:flex-row sm:items-center">
            <span>{t("shown", { shown: visible.length, total: rows.length })}</span>
            {visible.length < rows.length && (
              <button
                type="button"
                onClick={() => setLimit((current) => current + PAGE)}
                className="min-h-11 rounded-md border border-border px-3 text-foreground hover:bg-secondary md:min-h-9"
              >
                {t("more")}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// Короткие живые цифры последнего сбора: меняются вместе с данными.
function Summary({ data }: { data: WoltComparison }) {
  const t = useTranslations("wolt");
  const locale = useLocale();
  const n = (value: number) => value.toLocaleString(locale === "en" ? "en-GB" : "ru-RU");
  // Найдено на Wolt: каталог владельца — его сайт, он в этой плитке не нужен.
  const found = data.venues
    .filter((venue) => !venue.is_owner)
    .reduce((sum, venue) => sum + venue.items, 0);
  const titles = Object.fromEntries(data.venues.map((venue) => [venue.venue, venue.title]));
  // Сервер ставит владельца первым в паре (`a`), если он в ней есть.
  const owner = data.venues.find((venue) => venue.is_owner)?.venue;
  const ownerPairs = data.pairs.filter((pair) => pair.a === owner);
  const otherPairs = data.pairs.filter((pair) => pair.a !== owner);
  const tiles = [
    { label: t("stat_found"), value: found },
    { label: t("stat_comparable"), value: data.totals.rows },
    { label: t("stat_everywhere"), value: data.totals.all_venues },
    { label: t("stat_differ"), value: data.totals.differ },
  ];
  return (
    <section className="space-y-2">
      <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {tiles.map((tile) => (
          <div key={tile.label} className="rounded-md border border-border p-3">
            <dt className="text-xs text-muted-foreground">{tile.label}</dt>
            <dd className="mt-1 text-2xl font-semibold tabular-nums">{n(tile.value)}</dd>
          </div>
        ))}
      </dl>
      <ul className="space-y-1 text-sm">
        {ownerPairs.map((pair) => (
          <li key={`${pair.a}|${pair.b}`}>
            {t("owner_pair", {
              owner: titles[pair.a] ?? pair.a,
              other: titles[pair.b] ?? pair.b,
              common: n(pair.common),
              cheaper: n(pair.a_cheaper),
              dearer: n(pair.b_cheaper),
              same: n(pair.same_price),
            })}
          </li>
        ))}
      </ul>
      <ul className="flex flex-col gap-1 text-xs text-muted-foreground md:flex-row md:flex-wrap md:gap-x-6">
        {otherPairs.map((pair) => (
          <li key={`${pair.a}|${pair.b}`}>
            {t("pair_short", {
              a: titles[pair.a] ?? pair.a,
              b: titles[pair.b] ?? pair.b,
              common: n(pair.common),
              differ: n(pair.a_cheaper + pair.b_cheaper),
            })}
          </li>
        ))}
      </ul>
    </section>
  );
}

function rowKey(row: WoltRow): string {
  return Object.values(row.prices)
    .map((offer) => offer.url)
    .join("|");
}

function VenueCard({ venue, locale }: { venue: WoltVenue; locale: string }) {
  const t = useTranslations("wolt");
  return (
    <div className={cn("rounded-md border p-3", venue.is_owner ? "border-primary bg-primary/5" : "border-border")}>
      {venue.is_owner && <div className="mb-1 text-xs font-medium text-primary">{t("owner_badge")}</div>}
      <a
        href={venue.url}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1 text-sm font-medium hover:underline"
      >
        {venue.title}
        <ExternalLink className="h-3 w-3" aria-hidden="true" />
      </a>
      <div className="mt-1 text-xs text-muted-foreground">
        {t("items", { n: venue.items })}
        {" · "}
        {venue.last_ok_at ? t("updated", { when: formatRelative(venue.last_ok_at, locale) }) : t("never")}
      </div>
      {venue.last_status === "failed" && (
        <div className="mt-1 text-xs text-destructive">{t("failed")}</div>
      )}
    </div>
  );
}

function PriceCell({
  offer,
  venueTitle,
  cheapest,
  locale,
}: {
  offer: WoltOfferPrice | undefined;
  venueTitle: string;
  cheapest: boolean;
  locale: string;
}) {
  const t = useTranslations("wolt");
  if (!offer) return <span className="text-muted-foreground">—</span>;
  const price = formatPrice(offer.price, locale);
  return (
    <a
      href={offer.url}
      target="_blank"
      rel="noopener noreferrer"
      title={offer.name}
      aria-label={t("price_label", { venue: venueTitle, name: offer.name, price })}
      className={cn(
        "inline-flex flex-col items-end tabular-nums hover:underline",
        cheapest && "font-semibold text-success",
        !offer.in_stock && "text-muted-foreground",
      )}
    >
      <span>
        {price}
        {cheapest && <span className="sr-only"> ({t("cheapest")})</span>}
      </span>
      {offer.original_price != null && (
        <span className="text-xs text-muted-foreground line-through">
          {formatPrice(offer.original_price, locale)}
        </span>
      )}
      {!offer.in_stock && <span className="text-xs">{t("out_of_stock")}</span>}
    </a>
  );
}
