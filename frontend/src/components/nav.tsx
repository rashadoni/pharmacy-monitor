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
  Pill,
  Search,
  Settings,
  Star,
  TrendingUp,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LocaleSwitcher } from "@/components/locale-switcher";

const NAV_OVERVIEW = [
  { href: "/overview", key: "overview", icon: TrendingUp },
  { href: "/comparison", key: "comparison", icon: Search },
  { href: "/category-comparison", key: "category_comparison", icon: ListTree },
  { href: "/analytics", key: "analytics", icon: BarChart3 },
] as const;

const NAV_SITES = [
  { href: "/site/pharmonline", label: "pharmonline.az", icon: Pill },
  { href: "/site/aptekonline", label: "aptekonline.az", icon: Building2 },
  { href: "/site/aloe", label: "aloe.az", icon: Leaf },
] as const;

const NAV_ACTIONS = [
  { href: "/alerts", key: "alerts", icon: Bell },
  { href: "/watchlist", key: "watchlist", icon: Star },
  { href: "/matches/review", key: "matches_review", icon: Link2 },
] as const;

const NAV_SETTINGS = [
  { href: "/categories", key: "categories", icon: ListTree },
  { href: "/settings", key: "settings", icon: Settings },
] as const;

function NavLink({ href, icon: Icon, label, pathname }: {
  href: string; icon: React.ElementType; label: string; pathname: string | null;
}) {
  const active = pathname?.startsWith(href);
  return (
    <Link
      href={href}
      className={cn(
        "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
        active
          ? "bg-primary text-primary-foreground font-medium"
          : "text-foreground hover:bg-secondary",
      )}
    >
      <Icon className="h-4 w-4" />
      {label}
    </Link>
  );
}

function NavSection({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
        {label}
      </div>
      {children}
    </div>
  );
}

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
    <nav className="hidden md:flex flex-col w-56 shrink-0 border-r border-border bg-card p-4 gap-3 sticky top-0 h-screen self-start">
      <div className="px-2 py-3 mb-1">
        <div className="text-lg font-semibold text-foreground">{t("appName")}</div>
        <div className="text-xs text-muted-foreground">{t("tagline")}</div>
      </div>

      <div className="flex-1 min-h-0 flex flex-col gap-3 overflow-y-auto">
        <NavSection label={t("section_overview")}>
          {NAV_OVERVIEW.map((item) => (
            <NavLink key={item.href} href={item.href} icon={item.icon} label={t(item.key)} pathname={pathname} />
          ))}
        </NavSection>

        <NavSection label={t("section_sites")}>
          {NAV_SITES.map((item) => (
            <NavLink key={item.href} href={item.href} icon={item.icon} label={item.label} pathname={pathname} />
          ))}
        </NavSection>

        <NavSection label={t("section_actions")}>
          {NAV_ACTIONS.map((item) => (
            <NavLink key={item.href} href={item.href} icon={item.icon} label={t(item.key)} pathname={pathname} />
          ))}
        </NavSection>

        <NavSection label={t("section_settings")}>
          {NAV_SETTINGS.map((item) => (
            <NavLink key={item.href} href={item.href} icon={item.icon} label={t(item.key)} pathname={pathname} />
          ))}
        </NavSection>
      </div>

      <div className="border-t border-border pt-3 flex flex-col gap-2">
        <LocaleSwitcher />
        <button
          onClick={handleLogout}
          className="flex items-center gap-3 rounded-md px-3 py-2 text-sm text-destructive hover:bg-destructive/10 transition-colors"
        >
          <LogOut className="h-4 w-4" />
          {tAuth("logout")}
        </button>
      </div>
    </nav>
  );
}

/** Mobile bottom-tab nav (< md). */
export function BottomNav() {
  const pathname = usePathname();
  const t = useTranslations("nav");
  const visible = [
    { href: "/overview", key: "overview", icon: TrendingUp },
    { href: "/comparison", key: "comparison", icon: Search },
    { href: "/category-comparison", key: "category_comparison_short", icon: ListTree },
    { href: "/analytics", key: "analytics", icon: BarChart3 },
    { href: "/alerts", key: "alerts", icon: Bell },
    { href: "/settings", key: "settings", icon: Settings },
  ] as const;

  return (
    <nav className="md:hidden fixed bottom-0 left-0 right-0 z-50 border-t border-border bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80 grid grid-cols-6">
      {visible.map((item) => {
        const active = pathname?.startsWith(item.href);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            className={cn(
              "flex min-w-0 flex-col items-center justify-center gap-0.5 px-0.5 py-2.5 text-[10px] transition-colors",
              active ? "text-primary font-semibold" : "text-muted-foreground",
            )}
          >
            <Icon className="h-5 w-5 shrink-0" />
            <span className="max-w-full text-center leading-tight break-words">{t(item.key)}</span>
          </Link>
        );
      })}
    </nav>
  );
}
