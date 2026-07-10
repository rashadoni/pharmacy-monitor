"use client";

import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { useTranslations } from "next-intl";

type Theme = "light" | "dark";

/**
 * Dark mode toggle. Persists в localStorage ('pm_theme'), применяет .dark
 * класс на <html>. По умолчанию light. Synchronizes с system preference при
 * первом запуске если в localStorage нет значения.
 */
export function ThemeToggle() {
  const t = useTranslations("common");
  const [theme, setTheme] = useState<Theme>("light");
  const [mounted, setMounted] = useState(false);

  // Initial mount: read from localStorage or system
  useEffect(() => {
    const stored = localStorage.getItem("pm_theme") as Theme | null;
    const initial: Theme =
      stored ??
      (window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light");
    setTheme(initial);
    document.documentElement.classList.toggle("dark", initial === "dark");
    setMounted(true);
  }, []);

  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.classList.toggle("dark", next === "dark");
    localStorage.setItem("pm_theme", next);
  }

  // Не рендерим иконку до hydration чтобы избежать SSR mismatch
  if (!mounted) return <div className="h-4 w-4" />;

  const Icon = theme === "dark" ? Sun : Moon;
  return (
    <button
      onClick={toggle}
      className="rounded text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      title={theme === "dark" ? t("theme_light") : t("theme_dark")}
      aria-label={theme === "dark" ? t("theme_light") : t("theme_dark")}
    >
      <Icon className="h-4 w-4" />
    </button>
  );
}
