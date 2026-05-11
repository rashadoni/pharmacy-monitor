"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Bell, CheckCircle2, Globe, Key, LogOut, MessageCircle, Send, User, XCircle, Zap } from "lucide-react";
import { useState } from "react";
import { api, type NotifPrefs } from "@/lib/api";
import { LocaleSwitcher } from "@/components/locale-switcher";

const SEVERITIES = ["off", "info", "warning", "critical"] as const;

export default function SettingsPage() {
  const router = useRouter();
  const t = useTranslations("settings");
  const tCommon = useTranslations("common");
  const tAuth = useTranslations("auth");
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });

  async function handleLogout() {
    if (!confirm(tAuth("logout_confirm"))) return;
    await api.logout();
    router.push("/login");
  }

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
      </div>

      <Section title={t("profile")} icon={User}>
        {meQ.isLoading && <div className="text-muted-foreground">{tCommon("loading")}</div>}
        {meQ.data && (
          <div className="space-y-2">
            <Field label={t("email")} value={meQ.data.email} />
            {meQ.data.name && <Field label={t("name")} value={meQ.data.name} />}
            <Field label={t("role")} value={meQ.data.role} />
            <Field label={t("tenant_id")} value={String(meQ.data.tenant_id)} />
          </div>
        )}
      </Section>

      <NotificationsSection email={meQ.data?.email} />

      <ChangePasswordSection />

      <IntegrationsStatusSection />

      <Section title={t("language")} icon={Globe}>
        <LocaleSwitcher />
      </Section>

      <Section title={t("actions")}>
        <button
          onClick={handleLogout}
          className="inline-flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/5 text-destructive px-4 py-2 text-sm font-medium hover:bg-destructive/10"
        >
          <LogOut className="h-4 w-4" />
          {t("logout")}
        </button>
      </Section>
    </div>
  );
}

function ChangePasswordSection() {
  const [curr, setCurr] = useState("");
  const [next1, setNext1] = useState("");
  const [next2, setNext2] = useState("");
  const [done, setDone] = useState(false);

  const mut = useMutation({
    mutationFn: () => api.changePassword(curr, next1),
    onSuccess: () => {
      setDone(true);
      setCurr(""); setNext1(""); setNext2("");
      setTimeout(() => setDone(false), 5000);
    },
  });

  const mismatch = next1 && next2 && next1 !== next2;
  const tooShort = next1 && next1.length < 6;
  const canSubmit = curr && next1 && next2 && next1 === next2 && next1.length >= 6;

  return (
    <Section title="Сменить пароль" icon={Key}>
      <div className="space-y-3 max-w-md">
        <label className="block">
          <span className="text-xs font-medium block mb-1">Текущий пароль</span>
          <input
            type="password"
            value={curr}
            onChange={(e) => setCurr(e.target.value)}
            autoComplete="current-password"
            className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium block mb-1">Новый пароль (мин. 6 символов)</span>
          <input
            type="password"
            value={next1}
            onChange={(e) => setNext1(e.target.value)}
            autoComplete="new-password"
            className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
          />
          {tooShort && <span className="text-xs text-destructive">Минимум 6 символов</span>}
        </label>
        <label className="block">
          <span className="text-xs font-medium block mb-1">Повтори новый пароль</span>
          <input
            type="password"
            value={next2}
            onChange={(e) => setNext2(e.target.value)}
            autoComplete="new-password"
            className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
          />
          {mismatch && <span className="text-xs text-destructive">Пароли не совпадают</span>}
        </label>
        <button
          onClick={() => mut.mutate()}
          disabled={!canSubmit || mut.isPending}
          className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
        >
          {mut.isPending ? "Сохраняем…" : "Сменить пароль"}
        </button>
        {done && <div className="text-sm text-success">✓ Пароль изменён. Следующий вход — с новым.</div>}
        {mut.isError && (
          <div className="text-sm text-destructive">
            {(mut.error as any)?.message || "Не удалось сохранить — проверь текущий пароль"}
          </div>
        )}
      </div>
    </Section>
  );
}


function IntegrationsStatusSection() {
  const q = useQuery({ queryKey: ["integrations"], queryFn: api.integrations });

  if (q.isLoading || !q.data) {
    return null;
  }

  const items: { key: string; label: string; ok: boolean; hint: string }[] = [
    {
      key: "smtp",
      label: "SMTP (email-уведомления, отчёты, magic-link)",
      ok: q.data.smtp,
      hint: q.data.smtp ? `Активен (FROM: ${q.data.smtp_from ?? "—"})` : "Нужен SMTP_HOST + SMTP_PASSWORD",
    },
    {
      key: "telegram",
      label: "Telegram (push-алерты)",
      ok: q.data.telegram,
      hint: q.data.telegram
        ? `Bot готов${q.data.telegram_bot_username ? ` (@${q.data.telegram_bot_username})` : ""}`
        : "Нужен TELEGRAM_BOT_TOKEN от @BotFather",
    },
    {
      key: "sentry",
      label: "Sentry (error tracking)",
      ok: q.data.sentry,
      hint: q.data.sentry ? "Ошибки логируются" : "Нужен SENTRY_DSN от sentry.io",
    },
    {
      key: "scraperapi",
      label: "ScraperAPI (proxy для забаненных IP)",
      ok: q.data.scraperapi,
      hint: q.data.scraperapi
        ? `Активен для: ${q.data.scraperapi_sites.join(", ") || "(нет sites в env)"}`
        : "Опционально — для aptekonline на проде нужен Hobby $49/мес residential",
    },
  ];

  const okCount = items.filter((i) => i.ok).length;

  return (
    <Section title="Интеграции сервера" icon={Zap}>
      <div className="text-xs text-muted-foreground mb-3">
        Внешние сервисы, настроенные в <code className="font-mono">/etc/pharmacy-monitor/env</code>.
        Готово: <span className="font-semibold">{okCount} / {items.length}</span>.
        Полная настройка одной командой: <code className="font-mono">bash scripts/configure-integrations.sh</code>
      </div>
      <div className="space-y-2">
        {items.map((it) => (
          <div
            key={it.key}
            className={`flex items-start gap-2 rounded-md border px-3 py-2 text-sm ${
              it.ok ? "bg-success/5 border-success/30" : "bg-muted/40 border-border"
            }`}
          >
            {it.ok ? (
              <CheckCircle2 className="h-4 w-4 text-success mt-0.5 shrink-0" />
            ) : (
              <XCircle className="h-4 w-4 text-muted-foreground mt-0.5 shrink-0" />
            )}
            <div className="min-w-0 flex-1">
              <div className="font-medium">{it.label}</div>
              <div className="text-xs text-muted-foreground">{it.hint}</div>
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}


function NotificationsSection({ email }: { email?: string }) {
  const queryClient = useQueryClient();
  const t = useTranslations("settings");
  const tCommon = useTranslations("common");
  const prefsQ = useQuery({ queryKey: ["notif-prefs"], queryFn: api.notifPrefs });
  const integrationsQ = useQuery({ queryKey: ["integrations"], queryFn: api.integrations });
  const bindEmail = email ?? "your-email@example.com";
  const updateMutation = useMutation({
    mutationFn: (patch: Partial<NotifPrefs>) => api.notifPrefsUpdate(patch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notif-prefs"] }),
  });
  const unbindMutation = useMutation({
    mutationFn: () => api.notifUnbindTelegram(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notif-prefs"] }),
  });

  const prefs = prefsQ.data;

  return (
    <Section title={t("notifications")} icon={Bell}>
      {prefsQ.isLoading && <div className="text-muted-foreground">{tCommon("loading")}</div>}
      {prefs && (
        <div className="space-y-4">
          <div>
            <div className="text-sm font-medium mb-1.5 flex items-center gap-1.5">
              <MessageCircle className="h-4 w-4" />
              {t("email_notifs")}
            </div>
            <div className="text-xs text-muted-foreground mb-2">{t("email_notifs_hint")}</div>
            <SeveritySelect
              value={prefs.email_severity_min ?? "warning"}
              onChange={(v) => updateMutation.mutate({ email_severity_min: v })}
            />
          </div>

          <div className="pt-3 border-t border-border">
            <div className="text-sm font-medium mb-1.5 flex items-center gap-1.5">
              <Send className="h-4 w-4" />
              {t("telegram")}
            </div>
            {prefs.telegram_chat_id ? (
              <div className="space-y-2">
                <div className="text-xs text-muted-foreground">
                  {t("telegram_bound")} <code className="font-mono">{prefs.telegram_chat_id}</code>
                </div>
                <div className="flex items-center gap-2">
                  <SeveritySelect
                    value={prefs.telegram_severity_min ?? "critical"}
                    onChange={(v) => updateMutation.mutate({ telegram_severity_min: v })}
                  />
                  <button
                    onClick={() => {
                      if (confirm(t("telegram_unbind_confirm"))) unbindMutation.mutate();
                    }}
                    className="text-xs text-destructive hover:underline"
                  >
                    {t("telegram_unbind")}
                  </button>
                </div>
              </div>
            ) : !integrationsQ.data?.telegram ? (
              <div className="rounded-md bg-warning/10 border border-warning/40 p-3 text-sm">
                <div className="font-medium text-warning mb-1">⚠️ Telegram-бот не настроен на сервере</div>
                <div className="text-xs text-muted-foreground">
                  Чтобы привязать Telegram нужно сначала добавить <code className="font-mono">TELEGRAM_BOT_TOKEN</code> в
                  <code className="font-mono"> /etc/pharmacy-monitor/env</code>. Запусти:
                </div>
                <code className="block mt-2 font-mono text-xs bg-secondary/50 p-2 rounded">
                  bash scripts/configure-integrations.sh
                </code>
              </div>
            ) : (
              <div className="rounded-md bg-secondary/50 border border-border p-3 text-sm">
                <div>
                  {t("telegram_bind_hint")}{" "}
                  <code className="font-mono">/start {bindEmail}</code>
                </div>
                <a
                  href={`https://t.me/${
                    integrationsQ.data?.telegram_bot_username ??
                    process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME ??
                    "your_bot"
                  }`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-primary hover:underline mt-1 inline-block"
                >
                  {t("telegram_open_bot")}
                </a>
              </div>
            )}
          </div>

          <div className="pt-3 border-t border-border">
            <div className="text-sm font-medium mb-1.5">{t("quiet_hours")}</div>
            <div className="text-xs text-muted-foreground mb-2">{t("quiet_hours_hint")}</div>
            <input
              type="text"
              defaultValue={prefs.quiet_hours ?? ""}
              placeholder="22-08"
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (prefs.quiet_hours ?? "")) {
                  updateMutation.mutate({ quiet_hours: v || null });
                }
              }}
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm w-32 font-mono"
            />
          </div>

          <div className="pt-3 border-t border-border space-y-2">
            <div className="text-sm font-medium mb-1.5">{t("digest")}</div>
            <Toggle
              label={t("daily_digest")}
              checked={prefs.daily_digest}
              onChange={(v) => updateMutation.mutate({ daily_digest: v })}
            />
            <Toggle
              label={t("weekly_digest")}
              checked={prefs.weekly_digest}
              onChange={(v) => updateMutation.mutate({ weekly_digest: v })}
            />
          </div>

          {updateMutation.isPending && (
            <div className="text-xs text-muted-foreground">{t("saving")}</div>
          )}
          {updateMutation.isError && (
            <div className="text-xs text-destructive">{t("save_error")}</div>
          )}
        </div>
      )}
    </Section>
  );
}

function SeveritySelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const t = useTranslations("settings");
  const labels: Record<string, string> = {
    off: t("severity_off"),
    info: t("severity_info"),
    warning: t("severity_warning"),
    critical: t("severity_critical"),
  };
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
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
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 cursor-pointer text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4"
      />
      <span>{label}</span>
    </label>
  );
}

function Section({
  title,
  icon: Icon,
  children,
}: {
  title: string;
  icon?: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4 md:p-6">
      <h2 className="font-semibold mb-3 flex items-center gap-2">
        {Icon && <Icon className="h-4 w-4" />}
        {title}
      </h2>
      {children}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-3 gap-2 py-1">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="col-span-2 text-sm font-mono">{value}</div>
    </div>
  );
}
