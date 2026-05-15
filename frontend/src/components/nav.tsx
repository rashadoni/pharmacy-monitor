"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  BarChart3,
  Bell,
  Leaf,
  ListTree,
  LogOut,
  Search,
  Settings,
  Star,
  TrendingUp,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/comparison", key: "comparison", icon: Search },
  { href: "/overview", key: "overview", icon: TrendingUp },
  { href: "/aloe", key: "aloe", icon: Leaf },
  { href: "/analytics", key: "analytics", icon: BarChart3 },
  { href: "/alerts", key: "alerts", icon: Bell },
  { href: "/watchlist", key: "watchlist", icon: Star },
  { href: "/categories", key: "categories", icon: ListTree },
  { href: "/settings", key: "settings", icon: Settings },
] as const;

/** Desktop side-nav (>= md). */
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
    <nav className="hidden md:flex flex-col w-56 shrink-0 border-r border-border bg-card p-4 gap-1">
      <div className="px-2 py-3 mb-2">
        <div className="text-lg font-semibold text-foreground">{t("appName")}</div>
        <div className="text-xs text-muted-foreground">{t("tagline")}</div>
      </div>
      <div className="flex-1 flex flex-col gap-1">
        {NAV_ITEMS.map((item) => {
          const active = pathname?.startsWith(item.href);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-primary text-primary-foreground font-medium"
                  : "text-foreground hover:bg-secondary",
              )}
            >
              <Icon className="h-4 w-4" />
              {t(item.key)}
            </Link>
          );
        })}
      </div>
      <button
        onClick={handleLogout}
        className="flex items-center gap-3 rounded-md px-3 py-2 text-sm text-destructive hover:bg-destructive/10 transition-colors mt-2 border-t border-border pt-3"
      >
        <LogOut className="h-4 w-4" />
        {tAuth("logout")}
      </button>
    </nav>
  );
}

/** Mobile bottom-tab nav (< md). */
export function BottomNav() {
  const pathname = usePathname();
  const t = useTranslations("nav");
  const visible = NAV_ITEMS.slice(0, 5);

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
            {t(item.key)}
          </Link>
        );
      })}
    </nav>
  );
}
