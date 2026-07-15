"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Mail, Play, X, Zap } from "lucide-react";
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
      <DropdownMenu.Root open={open} onOpenChange={setOpen}>
        <DropdownMenu.Trigger asChild>
          <button
            type="button"
            className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-input bg-card px-3 py-1.5 text-sm hover:bg-muted/50 md:min-h-9"
            title={t("button_title")}
            aria-label={t("button_title")}
          >
            <Zap className="h-4 w-4 text-warning" aria-hidden="true" />
            {t("button_short")}
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            sideOffset={4}
            className="z-50 w-72 overflow-hidden rounded-lg border border-border bg-card shadow-lg"
          >
            <DropdownMenu.Item
              onSelect={() =>
                handleAction(
                  () => scrapeMut.mutate(),
                  t("scrape_confirm"),
                )
              }
              disabled={scrapeMut.isPending}
              className="flex min-h-11 cursor-pointer items-center gap-2.5 px-3 py-2.5 outline-none hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring data-[disabled]:pointer-events-none data-[disabled]:opacity-50"
            >
              <Play className="h-4 w-4 text-primary" aria-hidden="true" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">{t("scrape_title")}</div>
                <div className="text-xs text-muted-foreground">
                  {t("all_sites")} · {scrapeMut.isPending ? t("starting") : t("now")}
                </div>
              </div>
            </DropdownMenu.Item>

            <DropdownMenu.Item
              onSelect={() =>
                handleAction(
                  () => digestMut.mutate(),
                  t("digest_confirm"),
                )
              }
              disabled={digestMut.isPending}
              className="flex min-h-11 cursor-pointer items-center gap-2.5 border-t border-border px-3 py-2.5 outline-none hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring data-[disabled]:pointer-events-none data-[disabled]:opacity-50"
            >
              <Mail className="h-4 w-4 text-success" aria-hidden="true" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">{t("digest_title")}</div>
                <div className="text-xs text-muted-foreground">
                  {t("all_subscribers")} · {digestMut.isPending ? t("sending") : t("now")}
                </div>
              </div>
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      {feedback && (
        <div
          className="fixed bottom-4 right-4 z-50 flex max-w-xs items-start gap-2 rounded-lg border border-border bg-card p-3 text-sm shadow-lg"
          role="status"
          aria-live="polite"
        >
          <span className="flex-1">{feedback}</span>
          <button
            type="button"
            onClick={() => setFeedback(null)}
            className="-m-1 inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground md:h-8 md:w-8"
            aria-label={t("dismiss_feedback")}
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      )}
    </div>
  );
}
