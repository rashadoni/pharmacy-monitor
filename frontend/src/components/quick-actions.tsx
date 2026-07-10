"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Mail, Play, Zap } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { api, friendlyError } from "@/lib/api";

/**
 * Quick Actions dropdown в углу sidebar / страниц. Admin-only действия которые
 * раньше требовали SSH на прод:
 *   - Запустить scan сейчас (через scrape-request очередь)
 *   - Отправить test digest всем получателям с daily_digest=true
 */
export function QuickActions() {
  const t = useTranslations("quick_actions");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);

  const scrapeMut = useMutation({
    mutationFn: () => api.scrapeTrigger({ mode: "all" }),
    onSuccess: (r) => {
      setFeedback(t("scrape_queued", { id: r.id, status: r.status }));
      setOpen(false);
    },
    onError: (err) => setFeedback(t("error", { message: friendlyError(err, locale) })),
  });

  const digestMut = useMutation({
    mutationFn: () => api.digestSendTest("daily"),
    onSuccess: (r) => {
      setFeedback(t("digest_sent", { count: r.recipients_sent }));
      setOpen(false);
    },
    onError: (err) => setFeedback(t("error", { message: friendlyError(err, locale) })),
  });

  function handleAction(fn: () => void, confirmText: string) {
    if (!confirm(confirmText)) return;
    fn();
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1.5 rounded-md border border-input bg-card px-3 py-1.5 text-sm hover:bg-muted/50"
        title={t("button_title")}
      >
        <Zap className="h-4 w-4 text-warning" />
        {t("button_short")}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full mt-1 w-72 rounded-lg border border-border bg-card shadow-lg z-50 overflow-hidden">
            <button
              onClick={() =>
                handleAction(
                  () => scrapeMut.mutate(),
                  t("scrape_confirm"),
                )
              }
              disabled={scrapeMut.isPending}
              className="w-full text-left px-3 py-2.5 hover:bg-muted/50 flex items-center gap-2.5 disabled:opacity-50"
            >
              <Play className="h-4 w-4 text-primary" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">{t("scrape_title")}</div>
                <div className="text-xs text-muted-foreground">
                  {t("all_sites")} · {scrapeMut.isPending ? t("starting") : t("now")}
                </div>
              </div>
            </button>

            <button
              onClick={() =>
                handleAction(
                  () => digestMut.mutate(),
                  t("digest_confirm"),
                )
              }
              disabled={digestMut.isPending}
              className="w-full text-left px-3 py-2.5 hover:bg-muted/50 flex items-center gap-2.5 border-t border-border disabled:opacity-50"
            >
              <Mail className="h-4 w-4 text-success" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">{t("digest_title")}</div>
                <div className="text-xs text-muted-foreground">
                  {t("all_subscribers")} · {digestMut.isPending ? t("sending") : t("now")}
                </div>
              </div>
            </button>
          </div>
        </>
      )}

      {feedback && (
        <div
          className="fixed bottom-4 right-4 z-50 max-w-xs rounded-lg border border-border bg-card shadow-lg p-3 text-sm"
          onClick={() => setFeedback(null)}
          role="status"
        >
          {feedback}
          <div className="text-[10px] text-muted-foreground mt-1">
            {t("dismiss_feedback")}
          </div>
        </div>
      )}
    </div>
  );
}
