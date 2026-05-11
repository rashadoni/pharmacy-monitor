"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type CategoryRow } from "@/lib/api";

export default function CategoriesPage() {
  const [search, setSearch] = useState("");
  const [siteFilter, setSiteFilter] = useState<"" | "pharmonline" | "aptekonline" | "aloe">("");

  const { data, isLoading } = useQuery({
    queryKey: ["categories"],
    queryFn: api.categories,
  });

  const filtered = (data ?? []).filter((c) => {
    if (search && !`${c.label_ru} ${c.label_az ?? ""} ${c.key}`.toLowerCase().includes(search.toLowerCase())) {
      return false;
    }
    if (siteFilter === "pharmonline" && !c.pharmonline_slug) return false;
    if (siteFilter === "aptekonline" && !c.aptekonline_slug) return false;
    if (siteFilter === "aloe" && !c.aloe_slug) return false;
    return true;
  });

  const stats = {
    total: data?.length ?? 0,
    active: data?.filter((c) => c.is_active).length ?? 0,
    pharmonline: data?.filter((c) => c.pharmonline_slug).length ?? 0,
    aptekonline: data?.filter((c) => c.aptekonline_slug).length ?? 0,
    aloe: data?.filter((c) => c.aloe_slug).length ?? 0,
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">🗂️ Категории</h1>
        <p className="text-sm text-muted-foreground">
          Категории для скрейпинга. Каждая row — slug на одном из 3 сайтов.
        </p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Stat label="Всего" value={stats.total} />
        <Stat label="Active" value={stats.active} />
        <Stat label="pharmonline" value={stats.pharmonline} />
        <Stat label="aptekonline" value={stats.aptekonline} />
        <Stat label="aloe" value={stats.aloe} />
      </div>

      {/* Filters */}
      <div className="flex flex-col md:flex-row gap-2">
        <input
          type="search"
          placeholder="🔎 Поиск по названию / key"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
        />
        <select
          value={siteFilter}
          onChange={(e) => setSiteFilter(e.target.value as any)}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
        >
          <option value="">Все сайты</option>
          <option value="pharmonline">pharmonline</option>
          <option value="aptekonline">aptekonline</option>
          <option value="aloe">aloe</option>
        </select>
      </div>

      {isLoading && <div className="text-muted-foreground">Загрузка…</div>}

      <div className="rounded-lg border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left">Key</th>
              <th className="px-3 py-2 text-left">Label</th>
              <th className="px-3 py-2 text-left">pharmonline</th>
              <th className="px-3 py-2 text-left">aptekonline</th>
              <th className="px-3 py-2 text-left">aloe</th>
              <th className="px-3 py-2 text-center">Active</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((cat) => (
              <CategoryRowDesktop key={cat.id} cat={cat} />
            ))}
          </tbody>
        </table>
      </div>

      {filtered.length === 0 && !isLoading && (
        <div className="text-muted-foreground text-center py-4">
          По текущему фильтру ничего не найдено.
        </div>
      )}
    </div>
  );
}

function CategoryRowDesktop({ cat }: { cat: CategoryRow }) {
  return (
    <tr className="border-t border-border hover:bg-muted/30">
      <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{cat.key}</td>
      <td className="px-3 py-2">{cat.label_ru}</td>
      <td className="px-3 py-2 text-xs">
        {cat.pharmonline_slug ? (
          <span className="font-mono text-muted-foreground">{cat.pharmonline_slug}</span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-xs">
        {cat.aptekonline_slug ? (
          <span className="font-mono text-muted-foreground">{cat.aptekonline_slug}</span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-xs">
        {cat.aloe_slug ? (
          <span className="font-mono text-muted-foreground">{cat.aloe_slug}</span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </td>
      <td className="px-3 py-2 text-center">
        {cat.is_active ? (
          <span className="inline-flex rounded px-2 py-0.5 text-xs bg-success/10 text-success">
            ON
          </span>
        ) : (
          <span className="inline-flex rounded px-2 py-0.5 text-xs bg-muted text-muted-foreground">
            OFF
          </span>
        )}
      </td>
    </tr>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-border bg-card p-3">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="text-xl font-semibold mt-0.5 tabular-nums">{value}</div>
    </div>
  );
}
