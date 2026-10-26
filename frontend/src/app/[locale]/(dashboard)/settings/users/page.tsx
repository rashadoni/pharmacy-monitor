"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { useState } from "react";
import { ArrowLeft, Clock, KeyRound, Send, Trash2, UserPlus } from "lucide-react";
import {
  api,
  ApiError,
  friendlyError,
  type LoginLinkMail,
  type Recipient,
  type RecipientCreate,
  type RecipientCreated,
  type RecipientUpdate,
} from "@/lib/api";
import { loginLinkMailFromError, loginLinkMailText } from "@/lib/login-link-mail";
import {
  passwordProblem,
  passwordProblemInResponse,
  type PasswordProblem,
} from "@/lib/password-rule";
import { formatTime } from "@/lib/utils";

const SEVERITIES = ["off", "info", "warning", "critical"] as const;
// Тексты причин — общие со сменой своего пароля (пространство `settings`).
const PASSWORD_PROBLEM_TEXT = {
  password_not_text: "password_unusable",
  password_too_short: "password_too_short",
  password_too_long: "password_too_long",
} as const satisfies Record<PasswordProblem, string>;
type Sev = (typeof SEVERITIES)[number];

export default function UsersPage() {
  const t = useTranslations("users");
  const tCommon = useTranslations("common");
  const qc = useQueryClient();
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const listQ = useQuery({ queryKey: ["recipients"], queryFn: api.recipients });
  const statusQ = useQuery({ queryKey: ["system-status"], queryFn: api.systemStatus });
  const dailyEnabled = statusQ.data?.digests.daily.enabled ?? false;
  const weeklyEnabled = statusQ.data?.digests.weekly.enabled ?? true;

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
          <DigestScheduleNote status={statusQ.data?.digests} />
          <AddUserForm
            dailyEnabled={dailyEnabled}
            weeklyEnabled={weeklyEnabled}
            onAdd={(p) => createMut.mutate(p)}
            pending={createMut.isPending}
            error={
              createMut.isError ? ((createMut.error as Error)?.message ?? t("add_error")) : null
            }
            created={createMut.isSuccess ? createMut.data : null}
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
                dailyEnabled={dailyEnabled}
                weeklyEnabled={weeklyEnabled}
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

function DigestScheduleNote({ status }: { status?: import("@/lib/api").SystemStatus["digests"] }) {
  const t = useTranslations("users");
  return (
    <div className="rounded-lg border border-border bg-muted/40 p-3 text-xs text-muted-foreground flex items-start gap-2">
      <Clock className="h-4 w-4 mt-0.5 shrink-0" />
      <div>
        {t("digest_schedule_note")}
        <ul className="mt-1 list-disc pl-4 space-y-0.5">
          <li>{t("digest_schedule_daily_state", { state: status?.daily.enabled ? t("digest_schedule_enabled", { schedule: status.daily.schedule_baku }) : t("digest_schedule_disabled") })}</li>
          <li>{t("digest_schedule_weekly_state", { state: status?.weekly.enabled === false ? t("digest_schedule_disabled") : t("digest_schedule_enabled", { schedule: status?.weekly.schedule_baku ?? "Monday 10:00" }) })}</li>
        </ul>
      </div>
    </div>
  );
}

function AddUserForm({
  dailyEnabled,
  weeklyEnabled,
  onAdd,
  pending,
  error,
  created,
}: {
  dailyEnabled: boolean;
  weeklyEnabled: boolean;
  onAdd: (p: RecipientCreate) => void;
  pending: boolean;
  error: string | null;
  created?: RecipientCreated | null;
}) {
  const t = useTranslations("users");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<"admin" | "viewer">("viewer");
  const [daily, setDaily] = useState(false);
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
    setDaily(false);
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
          <Toggle label={t("daily_digest")} checked={daily} disabled={!dailyEnabled} onChange={setDaily} />
          <Toggle label={t("weekly_digest")} checked={weekly} disabled={!weeklyEnabled} onChange={setWeekly} />
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
        {created && <span className="text-xs text-success">{t("added", { email: created.email })}</span>}
        {error && <span className="text-xs text-destructive">{error}</span>}
      </div>
      {created?.invite_mail && <InviteMailNotice mail={created.invite_mail} />}
      <p className="text-xs text-muted-foreground mt-2">{t("add_hint")}</p>
    </div>
  );
}

/** Ушло ли письмо со ссылкой: «принято почтовым сервером» или почему нет. */
function MailNotice({ mail, retry }: { mail: LoginLinkMail; retry?: string }) {
  const t = useTranslations("users");
  if (mail.sent) {
    return (
      <p role="status" className="basis-full text-xs text-success">
        {loginLinkMailText(t, mail)}
        <span className="block text-muted-foreground">{t("mail_accepted_hint")}</span>
      </p>
    );
  }
  return (
    <p role="alert" className="basis-full text-xs text-destructive">
      {loginLinkMailText(t, mail)}
      {retry && <span className="block text-muted-foreground">{retry}</span>}
    </p>
  );
}

function InviteMailNotice({ mail }: { mail: LoginLinkMail }) {
  const t = useTranslations("users");
  return (
    <div className="mt-2">
      <MailNotice
        mail={mail}
        retry={t("invite_mail_retry", {
          button: t("send_login_link"),
          password: t("set_password"),
        })}
      />
    </div>
  );
}

function UserCard({
  u,
  isSelf,
  dailyEnabled,
  weeklyEnabled,
  onUpdate,
  onDelete,
  onHardDelete,
}: {
  u: Recipient;
  isSelf: boolean;
  dailyEnabled: boolean;
  weeklyEnabled: boolean;
  onUpdate: (patch: RecipientUpdate) => void;
  onDelete: () => void;
  onHardDelete: () => void;
}) {
  const t = useTranslations("users");
  const locale = useLocale();
  const sendLinkMut = useMutation({
    mutationFn: () => api.recipientSendLoginLink(u.id),
  });
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [passwordDone, setPasswordDone] = useState(false);
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
          disabled={!dailyEnabled}
          onChange={(daily_digest) => onUpdate({ daily_digest })}
        />
        <Toggle
          label={t("weekly_digest")}
          checked={u.weekly_digest}
          disabled={!weeklyEnabled}
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
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          <button
            onClick={() => {
              setPasswordOpen((open) => !open);
              setPasswordDone(false);
            }}
            disabled={isSelf || !u.is_active}
            title={
              isSelf
                ? t("set_password_self")
                : u.is_active
                  ? t("set_password_hint")
                  : t("send_login_link_inactive")
            }
            className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-input bg-background px-2.5 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed md:min-h-9"
          >
            <KeyRound className="h-3.5 w-3.5" />
            {t("set_password")}
          </button>
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
        {/* Без `mail` отвечает API старше этой страницы: о письме оно не знает. */}
        {sendLinkMut.isSuccess && sendLinkMut.data?.mail && (
          <MailNotice mail={sendLinkMut.data.mail} />
        )}
        {sendLinkMut.isError && <SendLinkError error={sendLinkMut.error} />}
        {passwordOpen && (
          <SetPasswordForm
            u={u}
            onCancel={() => setPasswordOpen(false)}
            onDone={() => {
              setPasswordOpen(false);
              setPasswordDone(true);
            }}
          />
        )}
        {passwordDone && (
          <p role="status" className="basis-full text-xs text-success">
            {t("set_password_done")}
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * Админ набирает пароль и сообщает его пользователю сам: письмом пароль не
 * уходит. Поле открытое — админ должен видеть, что именно он передаст.
 */
function SetPasswordForm({
  u,
  onCancel,
  onDone,
}: {
  u: Recipient;
  onCancel: () => void;
  onDone: () => void;
}) {
  const t = useTranslations("users");
  const tSettings = useTranslations("settings");
  const locale = useLocale();
  const [password, setPassword] = useState("");
  const mut = useMutation({
    mutationFn: () => api.recipientSetPassword(u.id, password),
    onSuccess: () => {
      setPassword("");
      onDone();
    },
  });
  // То же правило, что на сервере: кнопка не шлёт пароль, который он не примет.
  const problem = password ? passwordProblem(password) : null;
  let serverProblem: PasswordProblem | null = null;
  if (mut.error instanceof ApiError && mut.error.status === 422) {
    try {
      serverProblem = passwordProblemInResponse(JSON.parse(mut.error.detail));
    } catch {
      /* не JSON */
    }
  }
  const shownProblem = problem ?? serverProblem;

  return (
    <div className="basis-full rounded-md border border-border bg-muted/40 p-3">
      <label className="block">
        <span className="text-xs font-medium block mb-1">
          {t("set_password_label", { email: u.email })}
        </span>
        <input
          type="text"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            // Отказ сервера был о прежнем наборе — не держать его под новым.
            if (mut.isError) mut.reset();
          }}
          autoComplete="off"
          spellCheck={false}
          className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono md:min-h-9"
        />
      </label>
      {shownProblem && (
        <p className="mt-1 text-xs text-destructive">
          {tSettings(PASSWORD_PROBLEM_TEXT[shownProblem])}
        </p>
      )}
      {mut.isError && !serverProblem && (
        <p role="alert" className="mt-1 text-xs text-destructive">
          {t("set_password_error", { message: errorText(mut.error, locale) })}
        </p>
      )}
      <p className="mt-1 text-[11px] text-muted-foreground">
        {t("set_password_note", { active: t("active") })}
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          onClick={() => mut.mutate()}
          disabled={!password.trim() || !!problem || mut.isPending}
          className="min-h-11 rounded-md bg-primary text-primary-foreground px-3 py-1.5 text-xs font-medium hover:bg-primary/90 disabled:opacity-50 md:min-h-9"
        >
          {mut.isPending ? t("set_password_saving") : t("set_password_save")}
        </button>
        <button
          onClick={onCancel}
          className="min-h-11 rounded-md border border-input bg-background px-3 py-1.5 text-xs font-medium hover:bg-muted md:min-h-9"
        >
          {t("set_password_cancel")}
        </button>
      </div>
    </div>
  );
}

/**
 * Текст отказа API. Отказы этих ручек (400, 409) сервер пишет по-русски строкой
 * в `detail`; `friendlyError` показал бы в русском интерфейсе JSON целиком.
 */
function errorText(error: unknown, locale: string): string {
  if (error instanceof ApiError && locale === "ru") {
    try {
      const detail = JSON.parse(error.detail)?.detail;
      if (typeof detail === "string") return detail;
    } catch {
      /* не JSON */
    }
  }
  return friendlyError(error, locale);
}

/** Повторная отправка не удалась: письмо не ушло (424) или запрос не прошёл. */
function SendLinkError({ error }: { error: unknown }) {
  const t = useTranslations("users");
  const locale = useLocale();
  const mail = loginLinkMailFromError(error);
  if (mail) return <MailNotice mail={mail} />;
  return (
    <p role="alert" className="basis-full text-xs text-destructive">
      {t("login_link_error", { message: errorText(error, locale) })}
    </p>
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
