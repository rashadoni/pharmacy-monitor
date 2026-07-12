"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { useState } from "react";
import { ArrowLeft, Clock, Send, Trash2, UserPlus } from "lucide-react";
import { api, type Recipient, type RecipientCreate, type RecipientUpdate } from "@/lib/api";
import { formatTime } from "@/lib/utils";

const SEVERITIES = ["off", "info", "warning", "critical"] as const;
type Sev = (typeof SEVERITIES)[number];

export default function UsersPage() {
  const t = useTranslations("users");
  const tCommon = useTranslations("common");
  const qc = useQueryClient();
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const listQ = useQuery({ queryKey: ["recipients"], queryFn: api.recipients });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["recipients"] });
  const createMut = useMutation({
    mutationFn: (p: RecipientCreate) => api.recipientCreate(p),
    onSuccess: invalidate,
  });
  const updateMut = useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: RecipientUpdate }) =>
      api.recipientUpdate(id, patch),
    onSuccess: () => {
      invalidate();
      qc.invalidateQueries({ queryKey: ["me"] }); // self-edit → освежить роль/имя
    },
  });
  const deleteMut = useMutation({
    mutationFn: ({ id, hard }: { id: number; hard: boolean }) => api.recipientDelete(id, hard),
    onSuccess: invalidate,
  });

  const isAdmin = meQ.data?.role === "admin";

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <Link
          href="/settings"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground mb-2"
        >
          <ArrowLeft className="h-4 w-4" /> {t("back")}
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
      </div>

      {meQ.isError && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
          {t("load_error")}
        </div>
      )}

      {meQ.data && !isAdmin && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 p-4 text-sm">
          {t("admin_only")}
        </div>
      )}

      {isAdmin && (
        <>
          <DigestScheduleNote />
          <AddUserForm
            onAdd={(p) => createMut.mutate(p)}
            pending={createMut.isPending}
            error={
              createMut.isError ? ((createMut.error as Error)?.message ?? t("add_error")) : null
            }
            doneEmail={createMut.isSuccess ? createMut.data?.email : null}
          />
          <div className="space-y-3">
            {listQ.isLoading && (
              <div className="text-muted-foreground text-sm">{tCommon("loading")}</div>
            )}
            {listQ.isError && <div className="text-sm text-destructive">{t("load_error")}</div>}
            {listQ.data?.length === 0 && (
              <div className="text-sm text-muted-foreground">{t("empty")}</div>
            )}
            {listQ.data?.map((u) => (
              <UserCard
                key={u.id}
                u={u}
                isSelf={u.id === meQ.data?.id}
                onUpdate={(patch) => updateMut.mutate({ id: u.id, patch })}
                onDelete={() => {
                  if (confirm(t("delete_confirm", { email: u.email })))
                    deleteMut.mutate({ id: u.id, hard: false });
                }}
                onHardDelete={() => {
                  if (confirm(t("delete_permanently_confirm", { email: u.email })))
                    deleteMut.mutate({ id: u.id, hard: true });
                }}
              />
            ))}
          </div>
          {updateMut.isError && (
            <div className="text-sm text-destructive">
              {(updateMut.error as Error)?.message ?? t("save_error")}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function DigestScheduleNote() {
  const t = useTranslations("users");
  return (
    <div className="rounded-lg border border-border bg-muted/40 p-3 text-xs text-muted-foreground flex items-start gap-2">
      <Clock className="h-4 w-4 mt-0.5 shrink-0" />
      <div>
        {t("digest_schedule_note")}
        <ul className="mt-1 list-disc pl-4 space-y-0.5">
          <li>{t("digest_schedule_daily")}</li>
          <li>{t("digest_schedule_weekly")}</li>
        </ul>
      </div>
    </div>
  );
}

function AddUserForm({
  onAdd,
  pending,
  error,
  doneEmail,
}: {
  onAdd: (p: RecipientCreate) => void;
  pending: boolean;
  error: string | null;
  doneEmail?: string | null;
}) {
  const t = useTranslations("users");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<"admin" | "viewer">("viewer");
  const [daily, setDaily] = useState(true);
  const [weekly, setWeekly] = useState(false);

  const validEmail = /.+@.+\..+/.test(email.trim());

  function submit() {
    if (!validEmail) return;
    onAdd({
      email: email.trim().toLowerCase(),
      name: name.trim() || null,
      role,
      daily_digest: daily,
      weekly_digest: weekly,
    });
    setEmail("");
    setName("");
    setRole("viewer");
    setDaily(true);
    setWeekly(false);
  }

  return (
    <div className="rounded-lg border border-border bg-card p-4 md:p-5">
      <h2 className="font-semibold mb-3 flex items-center gap-2">
        <UserPlus className="h-4 w-4" />
        {t("add_title")}
      </h2>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="text-xs font-medium block mb-1">{t("col_email")}</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@company.az"
            className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm md:min-h-9"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium block mb-1">{t("col_name")}</span>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm md:min-h-9"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium block mb-1">{t("col_role")}</span>
          <RoleSelect value={role} onChange={setRole} />
        </label>
        <div className="flex items-end gap-4 pb-1">
          <Toggle label={t("daily_digest")} checked={daily} onChange={setDaily} />
          <Toggle label={t("weekly_digest")} checked={weekly} onChange={setWeekly} />
        </div>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <button
          onClick={submit}
          disabled={!validEmail || pending}
          className="min-h-11 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50 md:min-h-9"
        >
          {pending ? t("adding") : t("add_btn")}
        </button>
        {doneEmail && <span className="text-xs text-success">{t("added", { email: doneEmail })}</span>}
        {error && <span className="text-xs text-destructive">{error}</span>}
      </div>
      <p className="text-xs text-muted-foreground mt-2">{t("add_hint")}</p>
    </div>
  );
}

function UserCard({
  u,
  isSelf,
  onUpdate,
  onDelete,
  onHardDelete,
}: {
  u: Recipient;
  isSelf: boolean;
  onUpdate: (patch: RecipientUpdate) => void;
  onDelete: () => void;
  onHardDelete: () => void;
}) {
  const t = useTranslations("users");
  const locale = useLocale();
  const sendLinkMut = useMutation({
    mutationFn: () => api.recipientSendLoginLink(u.id),
  });
  return (
    <div
      className={`rounded-lg border p-4 ${
        u.is_active ? "border-border bg-card" : "border-border bg-muted/40 opacity-70"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium text-sm flex items-center gap-2 flex-wrap">
            <span className="truncate">{u.email}</span>
            {isSelf && (
              <span className="text-[10px] uppercase tracking-wide rounded bg-primary/10 text-primary px-1.5 py-0.5">
                {t("you")}
              </span>
            )}
            {!u.is_active && (
              <span className="text-[10px] uppercase tracking-wide rounded bg-muted text-muted-foreground px-1.5 py-0.5">
                {t("inactive")}
              </span>
            )}
          </div>
          {u.name && <div className="text-xs text-muted-foreground">{u.name}</div>}
          {u.last_login_at && (
            <div className="text-[11px] text-muted-foreground mt-0.5">
              {t("last_login")}: {formatTime(u.last_login_at, locale)}
            </div>
          )}
        </div>
        <button
          onClick={onDelete}
          disabled={isSelf}
          title={isSelf ? t("cant_delete_self") : t("delete")}
          className="text-muted-foreground hover:text-destructive disabled:opacity-30 disabled:hover:text-muted-foreground shrink-0 inline-flex min-h-11 min-w-11 items-center justify-center md:min-h-9 md:min-w-9"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="text-xs font-medium block mb-1">{t("col_role")}</span>
          <RoleSelect
            value={u.role}
            disabledViewer={isSelf}
            onChange={(role) => onUpdate({ role })}
          />
          {isSelf && <span className="text-[11px] text-muted-foreground">{t("cant_demote_self")}</span>}
        </label>
        <label className="block">
          <span className="text-xs font-medium block mb-1">{t("col_severity")}</span>
          <SeveritySelect
            value={(u.email_severity_min ?? "warning") as Sev}
            onChange={(email_severity_min) => onUpdate({ email_severity_min })}
          />
        </label>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 pt-3 border-t border-border">
        <Toggle
          label={t("daily_digest")}
          checked={u.daily_digest}
          onChange={(daily_digest) => onUpdate({ daily_digest })}
        />
        <Toggle
          label={t("weekly_digest")}
          checked={u.weekly_digest}
          onChange={(weekly_digest) => onUpdate({ weekly_digest })}
        />
        <Toggle
          label={t("active")}
          checked={u.is_active}
          disabled={isSelf}
          onChange={(is_active) => onUpdate({ is_active })}
        />
        {!u.is_active && !isSelf && (
          <button
            onClick={onHardDelete}
            title={t("delete_permanently_hint")}
            className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-destructive/40 text-destructive px-2.5 py-1.5 text-xs font-medium hover:bg-destructive/10 md:min-h-9"
          >
            <Trash2 className="h-3.5 w-3.5" />
            {t("delete_permanently")}
          </button>
        )}
        <div className="ml-auto flex items-center gap-2">
          {sendLinkMut.isSuccess && (
            <span className="text-[11px] text-success">{t("login_link_sent")}</span>
          )}
          {sendLinkMut.isError && (
            <span className="text-[11px] text-destructive">{t("login_link_error")}</span>
          )}
          <button
            onClick={() => sendLinkMut.mutate()}
            disabled={!u.is_active || sendLinkMut.isPending}
            title={u.is_active ? t("send_login_link_hint") : t("send_login_link_inactive")}
            className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-input bg-background px-2.5 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed md:min-h-9"
          >
            <Send className="h-3.5 w-3.5" />
            {sendLinkMut.isPending ? t("login_link_sending") : t("send_login_link")}
          </button>
        </div>
      </div>
    </div>
  );
}

function RoleSelect({
  value,
  onChange,
  disabledViewer,
}: {
  value: "admin" | "viewer";
  onChange: (v: "admin" | "viewer") => void;
  disabledViewer?: boolean;
}) {
  const t = useTranslations("users");
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as "admin" | "viewer")}
      className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm md:min-h-9"
    >
      <option value="admin">{t("role_admin")}</option>
      <option value="viewer" disabled={disabledViewer}>
        {t("role_viewer")}
      </option>
    </select>
  );
}

function SeveritySelect({ value, onChange }: { value: Sev; onChange: (v: Sev) => void }) {
  const t = useTranslations("settings");
  const labels: Record<Sev, string> = {
    off: t("severity_off"),
    info: t("severity_info"),
    warning: t("severity_warning"),
    critical: t("severity_critical"),
  };
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as Sev)}
      className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm md:min-h-9"
    >
      {SEVERITIES.map((s) => (
        <option key={s} value={s}>
          {labels[s]}
        </option>
      ))}
    </select>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label
      className={`flex min-h-11 items-center gap-2 text-sm md:min-h-9 ${disabled ? "opacity-40" : "cursor-pointer"}`}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4"
      />
      <span>{label}</span>
    </label>
  );
}
