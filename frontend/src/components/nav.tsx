"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  BarChart3,
  Bell,
  Building2,
  Leaf,
  Link2,
  ListTree,
  LogOut,
  Mail,
  Pill,
  Search,
  Settings,
  Star,
  TrendingUp,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ThemeToggle } from "./theme-toggle";

type NavItem = {
  href: string;
  key: string;
  icon: typeof Search;
};

// Группировка для desktop sidebar. На мобиле — flat первые 5.
const NAV_GROUPS: { label: string; items: readonly NavItem[] }[] = [
  {
    label: "Обзор",
    items: [
      { href: "/overview", key: "overview", icon: TrendingUp },
      { href: "/comparison", key: "comparison", icon: Search },
      { href: "/analytics", key: "analytics", icon: BarChart3 },
    ],
  },
  {
    label: "Сайты",
    items: [
      { href: "/site/pharmonline", key: "sitePharmonline", icon: Building2 },
      { href: "/site/aptekonline", key: "siteAptekonline", icon: Pill },
      { href: "/site/aloe", key: "siteAloe", icon: Leaf },
    ],
  },
  {
    label: "Действия",
    items: [
      { href: "/aloe-matcher", key: "aloeMatcher", icon: Link2 },
      { href: "/alerts", key: "alerts", icon: Bell },
      { href: "/watchlist", key: "watchlist", icon: Star },
    ],
  },
  {
    label: "Настройки",
    items: [
      { href: "/categories", key: "categories", icon: ListTree },
      { href: "/recipients", key: "recipients", icon: Mail },
      { href: "/settings", key: "settings", icon: Settings },
    ],
  },
];

const FLAT_ITEMS: readonly NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

/** Desktop side-nav (>= md). Grouped layout, ~30% компактнее. */
export function SideNav() {
  const pathname = usePathname();
  const router = useRouter();
  const t = useTranslations("nav");
  const tAuth = useTranslations("auth");

  async function handleLogout() {
    if (!confirm(tAuth("logout_confirm"))) return;
    await api.logout();
    router.push("/login");
  }

  return (
    <nav className="hidden md:flex flex-col w-60 shrink-0 border-r border-border bg-card p-4 gap-1 overflow-y-auto">
      <div className="px-2 py-3 mb-1 flex items-start justify-between gap-2">
        <div>
          <div className="text-lg font-semibold text-foreground">{t("appName")}</div>
          <div className="text-xs text-muted-foreground">{t("tagline")}</div>
        </div>
        <ThemeToggle />
      </div>

      <div className="flex-1 flex flex-col gap-3">
        {NAV_GROUPS.map((group) => (
          <div key={group.label} className="flex flex-col gap-0.5">
            <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted-foreground/60 font-semibold">
              {group.label}
            </div>
            {group.items.map((item) => {
              const active = pathname?.startsWith(item.href);
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={cn(
                    "flex items-center gap-3 rounded-md px-3 py-1.5 text-sm transition-colors",
                    active
                      ? "bg-primary text-primary-foreground font-medium"
                      : "text-foreground hover:bg-secondary",
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  <span className="truncate">{t(item.key)}</span>
                </Link>
              );
            })}
          </div>
        ))}
      </div>

      <button
        onClick={handleLogout}
        className="flex items-center gap-3 rounded-md px-3 py-2 text-xs text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors mt-2 border-t border-border pt-3"
      >
        <LogOut className="h-4 w-4" />
        {tAuth("logout")}
      </button>
    </nav>
  );
}

/** Mobile bottom-tab nav (< md). Top 5 most-used items. */
export function BottomNav() {
  const pathname = usePathname();
  const t = useTranslations("nav");
  const visible = FLAT_ITEMS.slice(0, 5);

  return (
    <nav className="md:hidden fixed bottom-0 left-0 right-0 z-50 border-t border-border bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80 grid grid-cols-5">
      {visible.map((item) => {
        const active = pathname?.startsWith(item.href);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            className={cn(
              "flex flex-col items-center justify-center gap-0.5 py-2.5 text-[10px] transition-colors",
              active ? "text-primary font-semibold" : "text-muted-foreground",
            )}
          >
            <Icon className="h-5 w-5" />
            <span className="truncate max-w-full px-1">{t(item.key)}</span>
          </Link>
        );
      })}
    </nav>
  );
}
