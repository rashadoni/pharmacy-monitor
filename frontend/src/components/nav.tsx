"use client";

import { Link, usePathname, useRouter } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  BarChart3,
  Activity,
  Bell,
  Building2,
  Leaf,
  Link2,
  ListTree,
  LogOut,
  MoreHorizontal,
  Pill,
  Search,
  Settings,
  Sparkles,
  Star,
  TrendingUp,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LocaleSwitcher } from "@/components/locale-switcher";

const NAV_OVERVIEW = [
  { href: "/overview", key: "overview", icon: TrendingUp },
  { href: "/runs", key: "runs", icon: Activity },
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
  { href: "/matcher", key: "matcher", icon: Sparkles },
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
      prefetch={false}
      className={cn(
        "flex min-h-11 items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
        active
          ? "bg-primary text-primary-foreground font-medium"
          : "text-foreground hover:bg-secondary",
      )}
    >
      <Icon className="h-4 w-4" aria-hidden="true" />
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
          type="button"
          onClick={handleLogout}
          className="flex min-h-11 items-center gap-3 rounded-md px-3 py-2 text-sm text-destructive transition-colors hover:bg-destructive/10"
        >
          <LogOut className="h-4 w-4" aria-hidden="true" />
          {tAuth("logout")}
        </button>
      </div>
    </nav>
  );
}

/** Mobile top bar keeps the active language visible and switchable. */
export function MobileHeader() {
  const t = useTranslations("nav");

  return (
    <header className="fixed inset-x-0 top-0 z-40 flex min-h-14 items-center justify-between border-b border-border bg-background/95 px-3 backdrop-blur supports-[backdrop-filter]:bg-background/80 md:hidden">
      <span className="truncate pr-2 text-sm font-semibold text-foreground">{t("appName")}</span>
      <LocaleSwitcher compact />
    </header>
  );
}

/** Mobile bottom-tab nav (< md). */
export function BottomNav() {
  const pathname = usePathname();
  const t = useTranslations("nav");
  const visible = [
    { href: "/overview", key: "overview", icon: TrendingUp },
    { href: "/comparison", key: "comparison", icon: Search },
    { href: "/matcher", key: "matcher_short", icon: Sparkles },
    { href: "/alerts", key: "alerts", icon: Bell },
  ] as const;
  const moreActive = !visible.some((item) => pathname?.startsWith(item.href));
  const moreGroups = [
    {
      label: t("section_overview"),
      items: [
        { href: "/runs", label: t("runs"), icon: Activity },
        { href: "/category-comparison", label: t("category_comparison"), icon: ListTree },
        { href: "/analytics", label: t("analytics"), icon: BarChart3 },
      ],
    },
    {
      label: t("section_sites"),
      items: NAV_SITES.map((item) => ({ href: item.href, label: item.label, icon: item.icon })),
    },
    {
      label: t("section_actions"),
      items: [
        { href: "/watchlist", label: t("watchlist"), icon: Star },
        { href: "/matches/review", label: t("matches_review"), icon: Link2 },
      ],
    },
    {
      label: t("section_settings"),
      items: [
        { href: "/categories", label: t("categories"), icon: ListTree },
        { href: "/settings", label: t("settings"), icon: Settings },
      ],
    },
  ];

  return (
    <nav className="fixed bottom-0 left-0 right-0 z-50 grid grid-cols-5 border-t border-border bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80 md:hidden">
      {visible.map((item) => {
        const active = pathname?.startsWith(item.href);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            prefetch={false}
            className={cn(
              "flex min-h-16 min-w-0 flex-col items-center justify-center gap-0.5 px-0.5 py-2.5 text-[10px] transition-colors",
              active ? "text-primary font-semibold" : "text-muted-foreground",
            )}
            aria-current={active ? "page" : undefined}
          >
            <Icon className="h-5 w-5 shrink-0" aria-hidden="true" />
            <span className="max-w-full text-center leading-tight break-words">{t(item.key)}</span>
          </Link>
        );
      })}

      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            type="button"
            className={cn(
              "flex min-h-16 min-w-0 flex-col items-center justify-center gap-0.5 px-1 py-2.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
              moreActive ? "font-semibold text-primary" : "text-muted-foreground",
            )}
            aria-label={t("more")}
          >
            <MoreHorizontal className="h-5 w-5 shrink-0" aria-hidden="true" />
            <span className="leading-tight">{t("more")}</span>
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            side="top"
            align="end"
            sideOffset={8}
            collisionPadding={8}
            className="z-50 max-h-[70vh] w-64 overflow-y-auto rounded-lg border border-border bg-card p-1 shadow-lg"
          >
            {moreGroups.map((group, groupIndex) => (
              <div key={group.label}>
                {groupIndex > 0 && <DropdownMenu.Separator className="my-1 h-px bg-border" />}
                <DropdownMenu.Label className="px-2 py-1 text-xs font-medium text-muted-foreground">
                  {group.label}
                </DropdownMenu.Label>
                {group.items.map((item) => {
                  const Icon = item.icon;
                  const active = pathname?.startsWith(item.href);
                  return (
                    <DropdownMenu.Item key={item.href} asChild>
                      <Link
                        href={item.href}
                        prefetch={false}
                        className={cn(
                          "flex min-h-11 cursor-pointer items-center gap-3 rounded-md px-2 py-2 text-sm outline-none hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                          active ? "font-medium text-primary" : "text-foreground",
                        )}
                        aria-current={active ? "page" : undefined}
                      >
                        <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                        <span>{item.label}</span>
                      </Link>
                    </DropdownMenu.Item>
                  );
                })}
              </div>
            ))}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </nav>
  );
}
