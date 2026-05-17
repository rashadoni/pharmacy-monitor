"use client";

/**
 * Onboarding баннер: показывается ОДИН раз пока пользователь не настроил ни
 * email-доставку, ни Telegram. P0.6 PO Audit 2026-05-17: 500 alert'ов
 * скопились в БД, никто их не читает — потому что delivery channels off.
 *
 * Логика:
 * - На каждом dashboard-экране вверху
 * - Скрыт если: email_severity_min ∈ {info, warning, critical} OR telegram_chat_id
 * - Скрыт если пользователь нажал × (запоминается в localStorage)
 * - CTA: Перейти в Настройки → /settings
 */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Bell, X } from "lucide-react";
import { api } from "@/lib/api";

const DISMISS_KEY = "notif-banner-dismissed-v1";

export function NotificationsBanner() {
  const [dismissed, setDismissed] = useState(true); // hide by default, set after hydration
  const { data } = useQuery({
    queryKey: ["notif-prefs-banner"],
    queryFn: () => api.notifPrefs(),
    staleTime: 60_000,
    retry: 0,
  });

  // SSR-safe: читаем localStorage только после mount
  useEffect(() => {
    setDismissed(localStorage.getItem(DISMISS_KEY) === "1");
  }, []);

  if (dismissed) return null;
  if (!data) return null;

  const emailOn =
    data.email_severity_min &&
    ["info", "warning", "critical"].includes(data.email_severity_min);
  const telegramOn = !!data.telegram_chat_id;
  if (emailOn || telegramOn) return null;

  function handleDismiss() {
    localStorage.setItem(DISMISS_KEY, "1");
    setDismissed(true);
  }

  return (
    <div className="relative rounded-lg border border-warning/30 bg-warning/5 px-4 py-3 mb-4 flex items-start gap-3">
      <Bell className="h-5 w-5 text-warning shrink-0 mt-0.5" aria-hidden />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-foreground">
          Алерты копятся, но никуда не доставляются
        </div>
        <div className="text-xs text-muted-foreground mt-0.5">
          Включи email-уведомления или Telegram, иначе ты не узнаешь когда
          конкуренты роняют цены или сайт перестал скрейпиться.{" "}
          <Link
            href="/settings"
            className="underline font-medium text-foreground hover:text-warning"
          >
            Настроить →
          </Link>
        </div>
      </div>
      <button
        onClick={handleDismiss}
        className="text-muted-foreground hover:text-foreground p-1 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label="Скрыть напоминание"
        title="Скрыть"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
