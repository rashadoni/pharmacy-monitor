"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Link, useRouter } from "@/i18n/navigation";
import { Bell, CheckCircle2, ChevronRight, DollarSign, Globe, Key, LogOut, MessageCircle, Send, User, Users, XCircle, Zap } from "lucide-react";
import { useEffect, useState } from "react";
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
          </div>
        )}
      </Section>

      <NotificationsSection />

      <ChangePasswordSection />

      <IntegrationsStatusSection />

      {/* Phase 4.6 — link to pricing settings sub-page */}
      <Section title={t("pricing_section_title")} icon={DollarSign}>
        <Link
          href="/settings/pricing"
          className="flex items-center justify-between rounded-md border border-border p-3 hover:bg-secondary/50 transition-colors group"
        >
          <div>
            <div className="font-medium text-sm">{t("pricing_link_label")}</div>
            <div className="text-xs text-muted-foreground mt-0.5">{t("pricing_link_hint")}</div>
          </div>
          <ChevronRight className="h-4 w-4 text-muted-foreground group-hover:text-foreground" />
        </Link>
      </Section>

      {meQ.data?.role === "admin" && (
        <Section title={t("users_section_title")} icon={Users}>
          <Link
            href="/settings/users"
            className="flex items-center justify-between rounded-md border border-border p-3 hover:bg-secondary/50 transition-colors group"
          >
            <div>
              <div className="font-medium text-sm">{t("users_link_label")}</div>
              <div className="text-xs text-muted-foreground mt-0.5">{t("users_link_hint")}</div>
            </div>
            <ChevronRight className="h-4 w-4 text-muted-foreground group-hover:text-foreground" />
          </Link>
        </Section>
      )}

      <Section title={t("language")} icon={Globe}>
        <LocaleSwitcher />
      </Section>

      <Section title={t("actions")}>
        <button
          onClick={handleLogout}
          className="inline-flex min-h-11 items-center gap-2 rounded-md border border-destructive/30 bg-destructive/5 text-destructive px-4 py-2 text-sm font-medium hover:bg-destructive/10 md:min-h-9"
        >
          <LogOut className="h-4 w-4" />
          {t("logout")}
        </button>
      </Section>
    </div>
  );
}

function ChangePasswordSection() {
  const t = useTranslations("settings");
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
    <Section title={t("change_password")} icon={Key}>
      <div className="space-y-3 max-w-md">
        <label className="block">
          <span className="text-xs font-medium block mb-1">{t("current_password")}</span>
          <input
            type="password"
            value={curr}
            onChange={(e) => setCurr(e.target.value)}
            autoComplete="current-password"
            className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm md:min-h-9"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium block mb-1">{t("new_password_hint")}</span>
          <input
            type="password"
            value={next1}
            onChange={(e) => setNext1(e.target.value)}
            autoComplete="new-password"
            className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm md:min-h-9"
          />
          {tooShort && <span className="text-xs text-destructive">{t("password_too_short")}</span>}
        </label>
        <label className="block">
          <span className="text-xs font-medium block mb-1">{t("new_password_repeat")}</span>
          <input
            type="password"
            value={next2}
            onChange={(e) => setNext2(e.target.value)}
            autoComplete="new-password"
            className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm md:min-h-9"
          />
          {mismatch && <span className="text-xs text-destructive">{t("password_mismatch")}</span>}
        </label>
        <button
          onClick={() => mut.mutate()}
          disabled={!canSubmit || mut.isPending}
          className="min-h-11 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50 md:min-h-9"
        >
          {mut.isPending ? t("saving") : t("change_password")}
        </button>
        {done && <div className="text-sm text-success">{t("password_changed")}</div>}
        {mut.isError && (
          <div className="text-sm text-destructive">
            {(mut.error as any)?.message || t("password_save_error")}
          </div>
        )}
      </div>
    </Section>
  );
}


function IntegrationsStatusSection() {
  const t = useTranslations("settings");
  const q = useQuery({ queryKey: ["integrations"], queryFn: api.integrations });

  if (q.isLoading || !q.data) {
    return null;
  }

  const items: { key: string; label: string; ok: boolean; hint: string }[] = [
    {
      key: "smtp",
      label: t("integration_smtp_label"),
      ok: q.data.smtp,
      hint: q.data.smtp
        ? t("integration_smtp_ok", { smtp_from: q.data.smtp_from ?? "—" })
        : t("integration_smtp_not_ok"),
    },
    {
      key: "telegram",
      label: t("integration_telegram_label"),
      ok: q.data.telegram,
      hint: q.data.telegram
        ? `${t("integration_telegram_ok")}${q.data.telegram_bot_username ? ` (@${q.data.telegram_bot_username})` : ""}`
        : t("integration_telegram_not_ok"),
    },
    {
      key: "sentry",
      label: t("integration_sentry_label"),
      ok: q.data.sentry,
      hint: q.data.sentry
        ? t("integration_sentry_ok")
        : t("integration_sentry_not_ok"),
    },
    {
      key: "scraperapi",
      label: t("integration_scraperapi_label"),
      ok: q.data.scraperapi,
      hint: q.data.scraperapi
        ? t("integration_scraperapi_ok", { sites: q.data.scraperapi_sites.join(", ") || "—" })
        : t("integration_scraperapi_not_ok"),
    },
    {
      key: "decodo",
      label: t("integration_decodo_label"),
      ok: q.data.decodo,
      hint: q.data.decodo
        ? t("integration_decodo_ok", {
            sites: q.data.decodo_sites.join(", ") || "—",
            pool: q.data.decodo_pool_size,
          })
        : t("integration_decodo_not_ok"),
    },
  ];

  const okCount = items.filter((i) => i.ok).length;

  return (
    <Section title={t("integrations")} icon={Zap}>
      <div className="text-xs text-muted-foreground mb-3">
        {t("integrations_desc")}{" "}
        {t("integrations_connected_count", { ok: okCount, total: items.length })}{" "}
        {t("integrations_optional_hint")}
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


function NotificationsSection() {
  const queryClient = useQueryClient();
  const t = useTranslations("settings");
  const tCommon = useTranslations("common");
  // Код привязки чата: выдаётся вошедшему пользователю, живёт недолго и
  // срабатывает один раз. Пока он на экране, настройки перечитываются — как
  // только бот привяжет чат, блок сам покажет привязку.
  const [bindCode, setBindCode] = useState<{ code: string; expiresAt: number } | null>(null);
  const prefsQ = useQuery({
    queryKey: ["notif-prefs"],
    queryFn: api.notifPrefs,
    refetchInterval: bindCode ? 3000 : false,
  });
  const integrationsQ = useQuery({ queryKey: ["integrations"], queryFn: api.integrations });
  const statusQ = useQuery({ queryKey: ["system-status"], queryFn: api.systemStatus });
  const botUsername = (
    integrationsQ.data?.telegram_bot_username ??
    process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME ??
    ""
  ).replace(/^@/, "");
  const updateMutation = useMutation({
    mutationFn: (patch: Partial<NotifPrefs>) => api.notifPrefsUpdate(patch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notif-prefs"] }),
  });
  const unbindMutation = useMutation({
    mutationFn: () => api.notifUnbindTelegram(),
    onSuccess: () => {
      setBindCode(null);
      queryClient.invalidateQueries({ queryKey: ["notif-prefs"] });
    },
  });
  const bindCodeMutation = useMutation({
    mutationFn: () => api.notifTelegramBindCode(),
    onSuccess: (issued) =>
      setBindCode({ code: issued.code, expiresAt: Date.now() + issued.expires_in_sec * 1000 }),
    // Отказ бывает и оттого, что чат уже привязан в другой вкладке.
    onError: () => queryClient.invalidateQueries({ queryKey: ["notif-prefs"] }),
  });
  useEffect(() => {
    if (!bindCode) return;
    // Код истёк: убрать его с экрана и перечитать настройки — привязка могла
    // пройти, пока вкладка была в фоне.
    const timer = setTimeout(() => {
      setBindCode(null);
      queryClient.invalidateQueries({ queryKey: ["notif-prefs"] });
    }, Math.max(0, bindCode.expiresAt - Date.now()));
    return () => clearTimeout(timer);
  }, [bindCode, queryClient]);

  const prefs = prefsQ.data;
  const telegramBound = !!prefs?.telegram_chat_id;
  useEffect(() => {
    // Чат привязан — код погашен, перечитывать настройки больше незачем.
    if (telegramBound) setBindCode(null);
  }, [telegramBound]);

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
                    className="inline-flex min-h-11 items-center text-xs text-destructive hover:underline md:min-h-9"
                  >
                    {t("telegram_unbind")}
                  </button>
                </div>
              </div>
            ) : !integrationsQ.data?.telegram ? (
              <div className="rounded-md bg-warning/10 border border-warning/40 p-3 text-sm">
                <div className="font-medium text-warning mb-1">{t("telegram_not_configured_title")}</div>
                <div className="text-xs text-muted-foreground">
                  {t("telegram_not_configured_hint")}{" "}
                  <code className="font-mono">TELEGRAM_BOT_TOKEN</code>{" "}
                  <code className="font-mono">/etc/pharmacy-monitor/env</code>
                </div>
                <code className="block mt-2 font-mono text-xs bg-secondary/50 p-2 rounded">
                  bash scripts/configure-integrations.sh
                </code>
              </div>
            ) : !botUsername ? (
              // Без имени бота код не выдаём: человек искал бы бота сам и мог
              // отправить код чужому боту с похожим именем.
              <div className="rounded-md bg-warning/10 border border-warning/40 p-3 text-sm">
                <div className="font-medium text-warning mb-1">{t("telegram_not_configured_title")}</div>
                <div className="text-xs text-muted-foreground">
                  {t("telegram_bot_username_missing")}{" "}
                  <code className="font-mono">TELEGRAM_BOT_USERNAME</code>{" "}
                  <code className="font-mono">/etc/pharmacy-monitor/env</code>
                </div>
              </div>
            ) : (
              <div className="rounded-md bg-secondary/50 border border-border p-3 text-sm space-y-2">
                <div>{t("telegram_bind_hint")}</div>
                {bindCode ? (
                  <>
                    <div>{t("telegram_bind_send", { bot: botUsername })}</div>
                    <code className="block font-mono text-xs bg-background border border-border p-2 rounded break-all select-all">
                      /start {bindCode.code}
                    </code>
                    <div className="text-xs text-muted-foreground">
                      {t("telegram_bind_valid_until", {
                        time: new Date(bindCode.expiresAt).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        }),
                      })}{" "}
                      {t("telegram_bind_waiting")}
                    </div>
                    <a
                      href={`https://t.me/${botUsername}?start=${bindCode.code}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex min-h-11 items-center rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 md:min-h-9"
                    >
                      {t("telegram_open_bot")}
                    </a>
                  </>
                ) : (
                  <button
                    onClick={() => bindCodeMutation.mutate()}
                    disabled={bindCodeMutation.isPending}
                    className="min-h-11 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50 md:min-h-9"
                  >
                    {t("telegram_bind_get_code")}
                  </button>
                )}
                {bindCodeMutation.isError && !bindCode && (
                  <div className="text-xs text-destructive">{t("telegram_bind_error")}</div>
                )}
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
              className="min-h-11 rounded-md border border-input bg-background px-3 py-2 text-sm w-32 font-mono md:min-h-9"
            />
          </div>

          <div className="pt-3 border-t border-border space-y-2">
            <div className="text-sm font-medium mb-1.5">{t("digest")}</div>
            <Toggle
              label={t("daily_digest_schedule", {
                time: statusQ.data?.digests.daily.schedule_baku ?? "09:00",
              })}
              checked={prefs.daily_digest}
              onChange={(v) => updateMutation.mutate({ daily_digest: v })}
              disabled={statusQ.data?.digests.daily.enabled === false}
            />
            {statusQ.data?.digests.daily.enabled === false && (
              <p className="text-xs text-warning">{t("daily_digest_disabled")}</p>
            )}
            <Toggle
              label={t("weekly_digest_schedule", {
                schedule: statusQ.data?.digests.weekly.schedule_baku ?? "Monday 10:00",
              })}
              checked={prefs.weekly_digest}
              onChange={(v) => updateMutation.mutate({ weekly_digest: v })}
              disabled={statusQ.data?.digests.weekly.enabled === false}
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
      className="min-h-11 rounded-md border border-input bg-background px-3 py-2 text-sm md:min-h-9"
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
  disabled = false,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className={`flex min-h-11 items-center gap-2 text-sm md:min-h-9 ${disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"}`}>
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
