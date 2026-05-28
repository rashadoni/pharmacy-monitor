"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Search, Trash2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import {
  api,
  type WatchlistCreatePayload,
  type WatchlistItem,
  type WatchlistLink,
} from "@/lib/api";
import { useDebounce } from "@/lib/use-debounce";

// Phase 5.2/UX audit (2026-05-28):
//  - Заменён raw fetch() → api.watchlist* (корректный X-Request-ID для Sentry).
//  - window.confirm() заменён на двухкликовое "armed" подтверждение — лучше UX
//    на мобилке, тестируется Playwright'ом, нет blocking dialog.
//  - Добавлен debounced search по canonical_name + brand.
//  - Типизация через WatchlistItem (раньше был any[]).
//  - Items_count placeholder показывает плюрализованное кол-во.

export default function WatchlistPage() {
  const t = useTranslations("watchlist");
  const tCommon = useTranslations("common");
  const queryClient = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 200);

  const { data, isLoading } = useQuery({
    queryKey: ["watchlist"],
    queryFn: api.watchlistList,
  });

  const deleteMutation = useMutation({
    mutationFn: api.watchlistDelete,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  const filtered = useMemo(() => {
    if (!data) return [] as WatchlistItem[];
    const q = debouncedSearch.trim().toLowerCase();
    if (!q) return data;
    return data.filter(
      (it) =>
        it.canonical_name.toLowerCase().includes(q) ||
        (it.brand ?? "").toLowerCase().includes(q),
    );
  }, [data, debouncedSearch]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("subtitle")}
            {data && data.length > 0 && (
              <span className="ml-2 text-xs text-muted-foreground/70">
                · {t("items_count", { count: data.length })}
              </span>
            )}
          </p>
        </div>
        <button
          onClick={() => setShowAdd(true)}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-3 py-2 text-sm font-medium hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          data-testid="watchlist-add"
        >
          <Plus className="h-4 w-4" />
          {t("add_button")}
        </button>
      </div>

      {showAdd && <AddForm onClose={() => setShowAdd(false)} />}

      {data && data.length > 0 && (
        <div className="relative max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("search_placeholder")}
            className="w-full rounded-md border border-input bg-background pl-9 pr-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="watchlist-search"
          />
        </div>
      )}

      {isLoading && <div className="text-muted-foreground">{tCommon("loading")}</div>}

      {data && data.length === 0 && (
        <div className="rounded-lg border border-dashed border-border bg-card p-8 text-center space-y-3">
          <div className="text-4xl">⭐</div>
          <div className="text-base font-medium">{t("empty_heading")}</div>
          <div className="text-sm text-muted-foreground max-w-md mx-auto leading-relaxed">
            {t("empty_desc")}
            <br />
            <span className="text-muted-foreground/70 text-xs">{t("empty_hint")}</span>
          </div>
          <button
            onClick={() => setShowAdd(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 mt-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Plus className="h-4 w-4" />
            {t("add_first")}
          </button>
        </div>
      )}

      {data && data.length > 0 && filtered.length === 0 && (
        <div className="text-sm text-muted-foreground text-center py-6">
          {t("no_match")}
        </div>
      )}

      <div className="space-y-2">
        {filtered.map((item) => (
          <WatchlistRow
            key={item.id}
            item={item}
            onDelete={() => deleteMutation.mutate(item.id)}
            deletePending={deleteMutation.isPending && deleteMutation.variables === item.id}
          />
        ))}
      </div>
    </div>
  );
}

function WatchlistRow({
  item,
  onDelete,
  deletePending,
}: {
  item: WatchlistItem;
  onDelete: () => void;
  deletePending: boolean;
}) {
  const t = useTranslations("watchlist");
  const tCommon = useTranslations("common");
  const [armed, setArmed] = useState(false);

  return (
    <div className="rounded-lg border border-border bg-card p-3 flex items-start gap-3">
      <div className="flex-1 min-w-0">
        <div className="font-medium text-sm">{item.canonical_name}</div>
        <div className="text-xs text-muted-foreground mt-0.5 flex flex-wrap gap-x-3">
          {item.brand && <span>{item.brand}</span>}
          {item.dosage && <span>{item.dosage}</span>}
          {item.pack_size && <span>{item.pack_size}</span>}
        </div>
        <div className="flex flex-wrap gap-2 mt-2">
          {item.links.map((link: WatchlistLink) => (
            <a
              key={link.site}
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              className={`text-xs px-2 py-0.5 rounded border ${
                link.status === "confirmed"
                  ? "border-success/40 bg-success/10 text-success"
                  : "border-border text-muted-foreground hover:bg-secondary"
              }`}
            >
              {link.site}
            </a>
          ))}
        </div>
      </div>
      {armed ? (
        <button
          onClick={onDelete}
          disabled={deletePending}
          onBlur={() => setArmed(false)}
          onKeyDown={(e) => {
            // Codex review fix (2026-05-28): Escape отменяет armed-режим.
            // Раньше keyboard-юзер мог "застрять" в armed state без явного
            // способа disarm без клика-куда-то-в-сторону.
            if (e.key === "Escape") {
              e.preventDefault();
              setArmed(false);
            }
          }}
          className="rounded border border-destructive bg-destructive/10 text-destructive px-2 py-1 text-xs font-medium hover:bg-destructive/20 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive"
          data-testid={`watchlist-delete-confirm-${item.id}`}
          autoFocus
        >
          {t("delete_arm")}
        </button>
      ) : (
        <button
          onClick={() => setArmed(true)}
          className="text-muted-foreground hover:text-destructive p-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded"
          title={tCommon("delete")}
          aria-label={t("delete_confirm", { name: item.canonical_name })}
          data-testid={`watchlist-delete-${item.id}`}
        >
          <Trash2 className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}

function AddForm({ onClose }: { onClose: () => void }) {
  const t = useTranslations("watchlist");
  const tCommon = useTranslations("common");
  const queryClient = useQueryClient();
  const [form, setForm] = useState<WatchlistCreatePayload>({
    canonical_name: "",
    brand: "",
    dosage: "",
    pack_size: "",
    pharmonline_url: "",
    aptekonline_url: "",
    aloe_url: "",
    notes: "",
  });

  const create = useMutation({
    mutationFn: (payload: WatchlistCreatePayload) => api.watchlistCreate(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      onClose();
    },
  });

  return (
    <div className="rounded-lg border border-border bg-card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">{t("form_title")}</h3>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Field
          label={t("form_canonical")}
          required
          value={form.canonical_name}
          onChange={(v) => setForm({ ...form, canonical_name: v })}
          placeholder="Friso Gold 1 800g"
          testId="watchlist-form-canonical"
        />
        <Field
          label={t("form_brand")}
          value={form.brand ?? ""}
          onChange={(v) => setForm({ ...form, brand: v })}
          placeholder="Friso"
        />
        <Field
          label={t("form_pack")}
          value={form.pack_size ?? ""}
          onChange={(v) => setForm({ ...form, pack_size: v })}
          placeholder="800q"
        />
        <Field
          label={t("form_dosage")}
          value={form.dosage ?? ""}
          onChange={(v) => setForm({ ...form, dosage: v })}
          placeholder={t("dosage_placeholder")}
        />
      </div>
      <div className="space-y-2 pt-2 border-t border-border">
        <div className="text-xs text-muted-foreground">{t("form_urls_label")}</div>
        <Field
          label="pharmonline.az"
          value={form.pharmonline_url ?? ""}
          onChange={(v) => setForm({ ...form, pharmonline_url: v })}
          placeholder="https://pharmonline.az/product/..."
        />
        <Field
          label="aptekonline.az"
          value={form.aptekonline_url ?? ""}
          onChange={(v) => setForm({ ...form, aptekonline_url: v })}
          placeholder="https://www.aptekonline.az/product/..."
        />
        <Field
          label="aloe.az"
          value={form.aloe_url ?? ""}
          onChange={(v) => setForm({ ...form, aloe_url: v })}
          placeholder="https://aloe.az/product/..."
        />
      </div>
      <div className="flex gap-2 pt-2">
        <button
          onClick={() => create.mutate(form)}
          disabled={!form.canonical_name || create.isPending}
          className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
          data-testid="watchlist-form-submit"
        >
          {create.isPending ? tCommon("save_view") : tCommon("save")}
        </button>
        <button
          onClick={onClose}
          className="text-sm text-muted-foreground hover:text-foreground"
        >
          {tCommon("cancel")}
        </button>
      </div>
      {create.isError && (
        <div className="text-sm text-destructive">{t("save_error")}</div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  required,
  testId,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  required?: boolean;
  testId?: string;
}) {
  return (
    <label className="block">
      <span className="text-xs font-medium block mb-1">
        {label}
        {required && <span className="text-destructive ml-0.5">*</span>}
      </span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        required={required}
        data-testid={testId}
        className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
    </label>
  );
}
