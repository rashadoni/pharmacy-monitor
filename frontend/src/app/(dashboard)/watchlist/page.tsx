"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import { api } from "@/lib/api";

export default function WatchlistPage() {
  const queryClient = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["watchlist"],
    queryFn: () =>
      fetch("/api/v1/dash/watchlist", { credentials: "include" }).then((r) => r.json()),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) =>
      fetch(`/api/v1/dash/watchlist/${id}`, { method: "DELETE", credentials: "include" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">📋 Watchlist</h1>
          <p className="text-sm text-muted-foreground">
            Priority SKU для гарантированного cross-site сравнения
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

      {showAdd && <AddForm onClose={() => setShowAdd(false)} />}

      {isLoading && <div className="text-muted-foreground">Загрузка…</div>}
      {data && data.length === 0 && (
        <div className="text-muted-foreground rounded-lg border border-dashed border-border p-8 text-center">
          Watchlist пуст. Нажми «Добавить» чтобы привязать товар к URL'ам на 3 сайтах.
        </div>
      )}

      <div className="space-y-2">
        {data?.map((item: any) => (
          <div
            key={item.id}
            className="rounded-lg border border-border bg-card p-3 flex items-start gap-3"
          >
            <div className="flex-1 min-w-0">
              <div className="font-medium text-sm">{item.canonical_name}</div>
              <div className="text-xs text-muted-foreground mt-0.5 flex flex-wrap gap-x-3">
                {item.brand && <span>{item.brand}</span>}
                {item.dosage && <span>{item.dosage}</span>}
                {item.pack_size && <span>{item.pack_size}</span>}
              </div>
              <div className="flex flex-wrap gap-2 mt-2">
                {item.links?.map((link: any) => (
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
            <button
              onClick={() => {
                if (confirm(`Удалить «${item.canonical_name}»?`)) {
                  deleteMutation.mutate(item.id);
                }
              }}
              className="text-muted-foreground hover:text-destructive p-1"
              title="Удалить"
            >
              <Trash2 className="h-4 w-4" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function AddForm({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
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
    mutationFn: (payload: typeof form) =>
      fetch("/api/v1/dash/watchlist", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then((r) => {
        if (!r.ok) throw new Error("Failed");
        return r.json();
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      onClose();
    },
  });

  return (
    <div className="rounded-lg border border-border bg-card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">Новый watchlist item</h3>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Field
          label="Название (canonical)"
          required
          value={form.canonical_name}
          onChange={(v) => setForm({ ...form, canonical_name: v })}
          placeholder="Friso Gold 1 800g"
        />
        <Field
          label="Бренд"
          value={form.brand}
          onChange={(v) => setForm({ ...form, brand: v })}
          placeholder="Friso"
        />
        <Field
          label="Pack size"
          value={form.pack_size}
          onChange={(v) => setForm({ ...form, pack_size: v })}
          placeholder="800q"
        />
        <Field
          label="Dosage"
          value={form.dosage}
          onChange={(v) => setForm({ ...form, dosage: v })}
          placeholder="(если применимо)"
        />
      </div>
      <div className="space-y-2 pt-2 border-t border-border">
        <div className="text-xs text-muted-foreground">URL на каждом сайте (опционально)</div>
        <Field
          label="pharmonline.az"
          value={form.pharmonline_url}
          onChange={(v) => setForm({ ...form, pharmonline_url: v })}
          placeholder="https://pharmonline.az/product/..."
        />
        <Field
          label="aptekonline.az"
          value={form.aptekonline_url}
          onChange={(v) => setForm({ ...form, aptekonline_url: v })}
          placeholder="https://www.aptekonline.az/product/..."
        />
        <Field
          label="aloe.az"
          value={form.aloe_url}
          onChange={(v) => setForm({ ...form, aloe_url: v })}
          placeholder="https://aloe.az/product/..."
        />
      </div>
      <div className="flex gap-2 pt-2">
        <button
          onClick={() => create.mutate(form)}
          disabled={!form.canonical_name || create.isPending}
          className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
        >
          {create.isPending ? "Сохраняем…" : "Сохранить"}
        </button>
        <button onClick={onClose} className="text-sm text-muted-foreground hover:text-foreground">
          Отмена
        </button>
      </div>
      {create.isError && (
        <div className="text-sm text-destructive">Не удалось сохранить</div>
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
