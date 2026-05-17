"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Mail, Plus, Trash2, UserX } from "lucide-react";
import { api, friendlyError, type Recipient, type RecipientCreate } from "@/lib/api";
import { formatTime } from "@/lib/utils";

type Severity = "off" | "info" | "warning" | "critical";

export default function RecipientsPage() {
  const [showCreate, setShowCreate] = useState(false);
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const recipientsQ = useQuery({
    queryKey: ["recipients"],
    queryFn: api.recipients,
  });
  const currentUserId = meQ.data?.id ?? -1;

  return (
    <div className="space-y-6">
      <header className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <Mail className="h-6 w-6 text-primary" /> Получатели email-digest
          </h1>
          <p className="text-sm text-muted-foreground max-w-2xl">
            Список email-ов кому приходит утренняя сводка алертов в 08:00 (Asia/Baku).
            Каждый получатель может быть admin (доступ ко всему дашборду) или viewer
            (только письма + read-only страницы).
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="inline-flex items-center gap-2 rounded-md bg-primary text-primary-foreground px-3 py-2 text-sm font-medium hover:bg-primary/90"
        >
          <Plus className="h-4 w-4" /> Добавить получателя
        </button>
      </header>

      {recipientsQ.isLoading && (
        <div className="text-sm text-muted-foreground">Загрузка…</div>
      )}
      {recipientsQ.error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive">
          {recipientsQ.error instanceof Error
            ? recipientsQ.error.message
            : "Ошибка загрузки"}
          <div className="mt-1 text-xs text-muted-foreground">
            Эту страницу видят только администраторы (role=admin).
          </div>
        </div>
      )}

      {recipientsQ.data && (
        <>
          <div className="hidden md:block rounded-lg border border-border bg-card overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-muted-foreground text-xs uppercase tracking-wide">
                <tr>
                  <th className="px-3 py-2 text-left">Email</th>
                  <th className="px-3 py-2 text-left">Имя</th>
                  <th className="px-3 py-2 text-left">Роль</th>
                  <th className="px-3 py-2 text-center">Daily</th>
                  <th className="px-3 py-2 text-center">Weekly</th>
                  <th className="px-3 py-2 text-left">Severity ≥</th>
                  <th className="px-3 py-2 text-left">Создан</th>
                  <th className="px-3 py-2 w-10"></th>
                </tr>
              </thead>
              <tbody>
                {recipientsQ.data.map((r) => (
                  <RecipientRow
                    key={r.id}
                    recipient={r}
                    isSelf={r.id === currentUserId}
                  />
                ))}
                {recipientsQ.data.length === 0 && (
                  <tr>
                    <td
                      colSpan={8}
                      className="px-3 py-6 text-center text-muted-foreground"
                    >
                      Нет получателей. Добавьте первого через кнопку выше.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {/* Mobile cards */}
          <div className="md:hidden space-y-2">
            {recipientsQ.data.map((r) => (
              <RecipientCard key={r.id} recipient={r} />
            ))}
          </div>
        </>
      )}

      {showCreate && <CreateModal onClose={() => setShowCreate(false)} />}
    </div>
  );
}

function RecipientRow({
  recipient,
  isSelf,
}: {
  recipient: Recipient;
  isSelf: boolean;
}) {
  const qc = useQueryClient();
  const update = useMutation({
    mutationFn: (patch: Partial<Recipient>) =>
      api.recipientUpdate(recipient.id, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["recipients"] }),
  });
  const remove = useMutation({
    mutationFn: () => api.recipientDelete(recipient.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["recipients"] }),
  });

  function handleDelete() {
    if (
      !confirm(
        `Удалить получателя ${recipient.email}?\n\nОн перестанет получать письма и не сможет логиниться.`,
      )
    )
      return;
    remove.mutate();
  }

  const opacity = recipient.is_active ? "" : "opacity-50";

  return (
    <tr className={`border-t border-border hover:bg-muted/30 ${opacity}`}>
      <td className="px-3 py-2 font-mono text-xs">
        {recipient.email}
        {isSelf && (
          <span className="ml-2 text-[10px] rounded bg-primary/10 text-primary px-1.5 py-0.5 font-semibold uppercase">
            вы
          </span>
        )}
      </td>
      <td className="px-3 py-2 text-muted-foreground">{recipient.name ?? "—"}</td>
      <td className="px-3 py-2">
        <select
          value={recipient.role}
          onChange={(e) =>
            update.mutate({ role: e.target.value as "admin" | "viewer" })
          }
          disabled={isSelf}
          title={isSelf ? "Нельзя сменить себе роль" : "admin или viewer"}
          className="rounded border border-input bg-background px-2 py-1 text-xs disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <option value="admin">admin</option>
          <option value="viewer">viewer</option>
        </select>
      </td>
      <td className="px-3 py-2 text-center">
        <input
          type="checkbox"
          checked={recipient.daily_digest}
          onChange={(e) => update.mutate({ daily_digest: e.target.checked })}
          className="h-4 w-4"
        />
      </td>
      <td className="px-3 py-2 text-center">
        <input
          type="checkbox"
          checked={recipient.weekly_digest}
          onChange={(e) => update.mutate({ weekly_digest: e.target.checked })}
          className="h-4 w-4"
        />
      </td>
      <td className="px-3 py-2">
        <select
          value={recipient.email_severity_min ?? "warning"}
          onChange={(e) =>
            update.mutate({ email_severity_min: e.target.value as Severity })
          }
          className="rounded border border-input bg-background px-2 py-1 text-xs"
        >
          <option value="off">off</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="critical">critical</option>
        </select>
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {formatTime(recipient.created_at)}
      </td>
      <td className="px-3 py-2">
        <button
          onClick={handleDelete}
          disabled={!recipient.is_active || isSelf}
          className="text-muted-foreground hover:text-destructive disabled:opacity-30 disabled:cursor-not-allowed"
          title={
            isSelf
              ? "Нельзя удалить себя"
              : recipient.is_active
                ? "Удалить"
                : "Уже удалён (soft delete)"
          }
        >
          {recipient.is_active ? (
            <Trash2 className="h-4 w-4" />
          ) : (
            <UserX className="h-4 w-4" />
          )}
        </button>
      </td>
    </tr>
  );
}

function RecipientCard({ recipient }: { recipient: Recipient }) {
  const qc = useQueryClient();
  const update = useMutation({
    mutationFn: (patch: Partial<Recipient>) =>
      api.recipientUpdate(recipient.id, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["recipients"] }),
  });
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0">
          <div className="font-medium text-sm font-mono">{recipient.email}</div>
          <div className="text-xs text-muted-foreground">
            {recipient.name ?? "—"} · {recipient.role}
          </div>
        </div>
        <span
          className={`text-[10px] rounded px-1.5 py-0.5 font-semibold uppercase ${
            recipient.is_active
              ? "bg-success/10 text-success"
              : "bg-muted text-muted-foreground"
          }`}
        >
          {recipient.is_active ? "active" : "inactive"}
        </span>
      </div>
      <div className="flex items-center gap-3 text-xs">
        <label className="inline-flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={recipient.daily_digest}
            onChange={(e) => update.mutate({ daily_digest: e.target.checked })}
            className="h-3.5 w-3.5"
          />
          Daily
        </label>
        <label className="inline-flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={recipient.weekly_digest}
            onChange={(e) => update.mutate({ weekly_digest: e.target.checked })}
            className="h-3.5 w-3.5"
          />
          Weekly
        </label>
      </div>
    </div>
  );
}

function CreateModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<"admin" | "viewer">("viewer");
  const [daily, setDaily] = useState(true);
  const [weekly, setWeekly] = useState(false);
  const [severity, setSeverity] = useState<Severity>("warning");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: (payload: RecipientCreate) => api.recipientCreate(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["recipients"] });
      onClose();
    },
    onError: (err: unknown) => {
      setError(friendlyError(err));
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    create.mutate({
      email: email.trim(),
      name: name.trim() || null,
      role,
      daily_digest: daily,
      weekly_digest: weekly,
      email_severity_min: severity,
    });
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <form
        onClick={(e) => e.stopPropagation()}
        onSubmit={handleSubmit}
        className="bg-card border border-border rounded-lg w-full max-w-md p-5 space-y-3"
      >
        <h2 className="text-lg font-semibold">Новый получатель</h2>

        <div>
          <label className="text-xs text-muted-foreground">Email *</label>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="client@company.az"
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
          />
        </div>

        <div>
          <label className="text-xs text-muted-foreground">Имя (опционально)</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Иван Петров"
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
          />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="text-xs text-muted-foreground">Роль</label>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as "admin" | "viewer")}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="viewer">viewer (только письма)</option>
              <option value="admin">admin (полный доступ)</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-muted-foreground">Severity ≥</label>
            <select
              value={severity}
              onChange={(e) => setSeverity(e.target.value as Severity)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="info">info (все события)</option>
              <option value="warning">warning (без info)</option>
              <option value="critical">critical (только важное)</option>
              <option value="off">off (не слать)</option>
            </select>
          </div>
        </div>

        <div className="flex items-center gap-4 text-sm">
          <label className="inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={daily}
              onChange={(e) => setDaily(e.target.checked)}
              className="h-4 w-4"
            />
            Daily digest (08:00 утра)
          </label>
          <label className="inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={weekly}
              onChange={(e) => setWeekly(e.target.checked)}
              className="h-4 w-4"
            />
            Weekly
          </label>
        </div>

        {error && (
          <div className="rounded-md bg-destructive/10 border border-destructive/30 p-2 text-xs text-destructive">
            {error}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-input px-3 py-2 text-sm hover:bg-muted/50"
          >
            Отмена
          </button>
          <button
            type="submit"
            disabled={create.isPending || !email.trim()}
            className="rounded-md bg-primary text-primary-foreground px-3 py-2 text-sm font-medium disabled:opacity-50 hover:bg-primary/90"
          >
            {create.isPending ? "Создаём…" : "Создать"}
          </button>
        </div>
      </form>
    </div>
  );
}
