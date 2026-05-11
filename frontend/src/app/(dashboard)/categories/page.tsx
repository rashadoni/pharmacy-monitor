"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import { api, type CategoryRow } from "@/lib/api";

/**
 * Извлечь slug категории из URL для каждого сайта.
 *
 * Поддерживаемые форматы:
 * - pharmonline: `https://pharmonline.az/products?category=ushaq-qidasi`
 * - aptekonline: `https://www.aptekonline.az/shop/productList?categoryId[]=252&lang=az`
 * - aloe: `https://aloe.az/catalog/filters/?category_slug=u%C5%9Faq-qidas%C4%B1`
 *
 * Если не parsится — возвращает исходный input (клиент мог ввести голый slug).
 */
function extractSlug(site: "pharmonline" | "aptekonline" | "aloe", input: string): string {
  const trimmed = input.trim();
  if (!trimmed) return "";
  // Если не похоже на URL — это уже slug
  if (!trimmed.startsWith("http")) return trimmed;
  try {
    const u = new URL(trimmed);
    if (site === "pharmonline") {
      return u.searchParams.get("category") ?? trimmed;
    }
    if (site === "aptekonline") {
      // categoryId[]=252 — array param
      const v = u.searchParams.get("categoryId[]") ?? u.searchParams.get("categoryId");
      return v ?? trimmed;
    }
    if (site === "aloe") {
      const v = u.searchParams.get("category_slug");
      return v ? decodeURIComponent(v) : trimmed;
    }
  } catch {
    return trimmed;
  }
  return trimmed;
}

/**
 * Слаг из label: «Витамины» → `vitaminy`.
 * Простой ASCII-only fallback; кириллица транслитерируется по таблице.
 */
function slugify(s: string): string {
  const map: Record<string, string> = {
    а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "yo", ж: "zh", з: "z",
    и: "i", й: "y", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r",
    с: "s", т: "t", у: "u", ф: "f", х: "kh", ц: "ts", ч: "ch", ш: "sh", щ: "sch",
    ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya",
  };
  return s
    .toLowerCase()
    .split("")
    .map((ch) => map[ch] ?? ch)
    .join("")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export default function CategoriesPage() {
  const [search, setSearch] = useState("");
  const [siteFilter, setSiteFilter] = useState<"" | "pharmonline" | "aptekonline" | "aloe">("");
  const [activeOnly, setActiveOnly] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [editing, setEditing] = useState<CategoryRow | null>(null);
  const queryClient = useQueryClient();

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
    if (activeOnly && !c.is_active) return false;
    return true;
  });

  // Категория «cross-2» — у неё есть pharm+apt-slug'и (двусторонний кейс).
  // «cross-3» — pharm+apt+aloe (полный треугольник, редко).
  const stats = {
    total: data?.length ?? 0,
    active: data?.filter((c) => c.is_active).length ?? 0,
    cross2: data?.filter((c) => c.pharmonline_slug && c.aptekonline_slug).length ?? 0,
    cross3: data?.filter((c) => c.pharmonline_slug && c.aptekonline_slug && c.aloe_slug).length ?? 0,
    pharmonline: data?.filter((c) => c.pharmonline_slug).length ?? 0,
    aptekonline: data?.filter((c) => c.aptekonline_slug).length ?? 0,
    aloe: data?.filter((c) => c.aloe_slug).length ?? 0,
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">🗂️ Категории</h1>
          <p className="text-sm text-muted-foreground">
            Категории для скрейпинга. ON-категории идут в next-run; OFF — пропускаются.
          </p>
        </div>
        <button
          onClick={() => setShowAdd(true)}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-3 py-2 text-sm font-medium hover:bg-primary/90"
        >
          <Plus className="h-4 w-4" />
          Добавить
        </button>
      </div>

      {showAdd && <CategoryForm onClose={() => setShowAdd(false)} />}
      {editing && (
        <CategoryForm
          editing={editing}
          onClose={() => setEditing(null)}
        />
      )}

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-7 gap-3">
        <Stat label="Всего" value={stats.total} />
        <Stat label="Active" value={stats.active} />
        <Stat label="Cross-2" value={stats.cross2} highlight />
        <Stat label="Cross-3" value={stats.cross3} highlight />
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
        <label className="inline-flex items-center gap-2 px-3 text-sm">
          <input
            type="checkbox"
            checked={activeOnly}
            onChange={(e) => setActiveOnly(e.target.checked)}
            className="rounded"
          />
          Только Active
        </label>
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
              <th className="px-3 py-2 text-center"></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((cat) => (
              <CategoryRowDesktop
                key={cat.id}
                cat={cat}
                onEdit={() => setEditing(cat)}
                onChanged={() =>
                  queryClient.invalidateQueries({ queryKey: ["categories"] })
                }
              />
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

function CategoryRowDesktop({
  cat,
  onEdit,
  onChanged,
}: {
  cat: CategoryRow;
  onEdit: () => void;
  onChanged: () => void;
}) {
  const queryClient = useQueryClient();

  const toggleActive = useMutation({
    mutationFn: () =>
      api.categoryUpdate(cat.id, {
        key: cat.key,
        label_ru: cat.label_ru,
        label_az: cat.label_az,
        pharmonline_slug: cat.pharmonline_slug,
        aptekonline_slug: cat.aptekonline_slug,
        aloe_slug: cat.aloe_slug,
        is_active: !cat.is_active,
      }),
    onSuccess: onChanged,
  });

  const remove = useMutation({
    mutationFn: () => api.categoryDelete(cat.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["categories"] }),
  });

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
        <button
          onClick={() => toggleActive.mutate()}
          disabled={toggleActive.isPending}
          className={`inline-flex rounded px-2 py-0.5 text-xs font-medium transition-opacity ${
            cat.is_active
              ? "bg-success/10 text-success hover:bg-success/20"
              : "bg-muted text-muted-foreground hover:bg-muted/80"
          } ${toggleActive.isPending ? "opacity-50" : ""}`}
        >
          {cat.is_active ? "ON" : "OFF"}
        </button>
      </td>
      <td className="px-3 py-2 text-center">
        <div className="inline-flex items-center gap-2">
          <button
            onClick={onEdit}
            className="text-muted-foreground hover:text-foreground"
            title="Редактировать"
          >
            <Pencil className="h-4 w-4" />
          </button>
          <button
            onClick={() => {
              if (confirm(`Удалить категорию «${cat.label_ru}»?`)) remove.mutate();
            }}
            disabled={remove.isPending}
            className="text-muted-foreground hover:text-destructive disabled:opacity-50"
            title="Удалить"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </td>
    </tr>
  );
}

function Stat({ label, value, highlight }: { label: string; value: number; highlight?: boolean }) {
  return (
    <div
      className={`rounded-lg border bg-card px-3 py-2 ${
        highlight ? "border-success/40 bg-success/5" : "border-border"
      }`}
    >
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  );
}

function CategoryForm({
  onClose,
  editing,
}: {
  onClose: () => void;
  editing?: CategoryRow;
}) {
  const queryClient = useQueryClient();
  const isEdit = Boolean(editing);
  const [form, setForm] = useState({
    label_ru: editing?.label_ru ?? "",
    label_az: editing?.label_az ?? "",
    // В edit-режиме pre-fill голым slug (без URL) — пусть пользователь видит
    // что было записано и при желании поверх вставит новый URL.
    pharmonline_url: editing?.pharmonline_slug ?? "",
    aptekonline_url: editing?.aptekonline_slug ?? "",
    aloe_url: editing?.aloe_slug ?? "",
  });
  const [error, setError] = useState<string | null>(null);

  const phmSlug = extractSlug("pharmonline", form.pharmonline_url);
  const aptSlug = extractSlug("aptekonline", form.aptekonline_url);
  const aloeSlug = extractSlug("aloe", form.aloe_url);
  // В edit оставляем существующий key (его менять рискованно и обычно не нужно).
  // В add — генерируем slug из label_ru.
  const key = isEdit ? editing!.key : slugify(form.label_ru || form.label_az);

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        key,
        label_ru: form.label_ru,
        label_az: form.label_az || null,
        pharmonline_slug: phmSlug || null,
        aptekonline_slug: aptSlug || null,
        aloe_slug: aloeSlug || null,
        is_active: editing?.is_active ?? true,
      };
      return isEdit
        ? api.categoryUpdate(editing!.id, payload).then(() => payload)
        : api.categoryCreate(payload);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["categories"] });
      onClose();
    },
    onError: (e: any) => setError(e?.message || "Не удалось сохранить"),
  });

  const canSubmit =
    form.label_ru.trim().length > 0 && (phmSlug || aptSlug || aloeSlug);

  return (
    <div className="rounded-lg border border-border bg-card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">
          {isEdit ? `Редактировать категорию #${editing!.id}` : "Новая категория"}
        </h3>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>
      <p className="text-xs text-muted-foreground">
        {isEdit
          ? "Можно поправить название или slug на любом сайте. Изменение slug повлияет только на следующий scrape — существующие продукты не удалятся."
          : "Вставь URL категории с каждого сайта — slug извлечётся автоматически. Можно ввести голый slug, если уже знаешь."}
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Field
          label="Название (RU)"
          required
          value={form.label_ru}
          onChange={(v) => setForm({ ...form, label_ru: v })}
          placeholder="Витамины"
        />
        <Field
          label="Название (AZ)"
          value={form.label_az}
          onChange={(v) => setForm({ ...form, label_az: v })}
          placeholder="Vitaminlər"
        />
      </div>

      <div className="space-y-2 pt-2 border-t border-border">
        <UrlField
          label="pharmonline.az"
          value={form.pharmonline_url}
          extractedSlug={phmSlug}
          onChange={(v) => setForm({ ...form, pharmonline_url: v })}
          placeholder="https://pharmonline.az/products?category=…"
        />
        <UrlField
          label="aptekonline.az"
          value={form.aptekonline_url}
          extractedSlug={aptSlug}
          onChange={(v) => setForm({ ...form, aptekonline_url: v })}
          placeholder="https://www.aptekonline.az/shop/productList?categoryId[]=…"
        />
        <UrlField
          label="aloe.az"
          value={form.aloe_url}
          extractedSlug={aloeSlug}
          onChange={(v) => setForm({ ...form, aloe_url: v })}
          placeholder="https://aloe.az/catalog/filters/?category_slug=…"
        />
      </div>

      {key && (
        <div className="text-xs text-muted-foreground">
          Key {isEdit ? "(read-only при редактировании)" : "(автоматически)"}:{" "}
          <span className="font-mono">{key}</span>
        </div>
      )}

      <div className="flex gap-2 pt-2">
        <button
          onClick={() => save.mutate()}
          disabled={!canSubmit || save.isPending}
          className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
        >
          {save.isPending ? "Сохраняем…" : isEdit ? "Применить" : "Сохранить"}
        </button>
        <button onClick={onClose} className="text-sm text-muted-foreground hover:text-foreground">
          Отмена
        </button>
      </div>
      {error && <div className="text-sm text-destructive">{error}</div>}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  required,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  required?: boolean;
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
        className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
    </label>
  );
}

function UrlField({
  label,
  value,
  extractedSlug,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  extractedSlug: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="block">
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-xs font-medium">{label}</span>
        {extractedSlug && (
          <span className="text-xs text-success font-mono">→ {extractedSlug}</span>
        )}
      </div>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring font-mono"
      />
    </label>
  );
}
