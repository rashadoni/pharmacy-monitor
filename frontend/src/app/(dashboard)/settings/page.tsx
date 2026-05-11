"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Bell, Globe, LogOut, MessageCircle, Send, User } from "lucide-react";
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

      <NotificationsSection />

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

function NotificationsSection() {
  const queryClient = useQueryClient();
  const t = useTranslations("settings");
  const tCommon = useTranslations("common");
  const prefsQ = useQuery({ queryKey: ["notif-prefs"], queryFn: api.notifPrefs });
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
            ) : (
              <div className="rounded-md bg-secondary/50 border border-border p-3 text-sm">
                <div>
                  {t("telegram_bind_hint")}{" "}
                  <code className="font-mono">/start your-email@example.com</code>
                </div>
                <a
                  href={`https://t.me/${process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME ?? "your_bot"}`}
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
