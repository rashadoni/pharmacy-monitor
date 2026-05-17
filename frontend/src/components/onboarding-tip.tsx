"use client";

/**
 * P1.6 (PO Audit 2026-05-17): inline onboarding hint.
 *
 * Простой self-contained компонент без tour-library (react-joyride и т.п.).
 * Каждая страница рендерит свой `<OnboardingTip id="..." ... />` — это
 * informational banner, dismiss переживает между сессиями (localStorage).
 *
 * Зачем не Joyride / Intro.js: мы НЕ хотим многошаговый guided tour (это
 * раздражает в дашборде, который пользователь будет открывать 100+ раз).
 * Хотим — точечную подсказку на странице, где она актуальна, dismissible.
 *
 * Использование:
 *   <OnboardingTip id="comparison-arrows" title="..." description="..." />
 */

import { useEffect, useState } from "react";
import { Lightbulb, X } from "lucide-react";

interface Props {
  id: string;
  title: string;
  description: React.ReactNode;
  /** ID можно версионировать (`-v2`) чтобы «оживить» подсказку после правок. */
}

export function OnboardingTip({ id, title, description }: Props) {
  const storageKey = `tip-${id}-dismissed`;
  const [dismissed, setDismissed] = useState(true); // SSR-safe: hide пока не hydrate

  useEffect(() => {
    setDismissed(localStorage.getItem(storageKey) === "1");
  }, [storageKey]);

  if (dismissed) return null;

  function handleDismiss() {
    localStorage.setItem(storageKey, "1");
    setDismissed(true);
  }

  return (
    <div className="relative rounded-lg border border-primary/30 bg-primary/5 px-4 py-3 flex items-start gap-3 text-sm">
      <Lightbulb className="h-5 w-5 text-primary shrink-0 mt-0.5" aria-hidden />
      <div className="flex-1 min-w-0">
        <div className="font-medium text-foreground">{title}</div>
        <div className="text-xs text-muted-foreground mt-0.5 leading-relaxed">
          {description}
        </div>
      </div>
      <button
        onClick={handleDismiss}
        className="text-muted-foreground hover:text-foreground p-1 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label="Скрыть подсказку"
        title="Скрыть навсегда"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
