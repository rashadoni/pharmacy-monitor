import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { SideNav, BottomNav, MobileHeader } from "@/components/nav";
import { NotificationsBanner } from "@/components/notifications-banner";
import { getTranslations } from "next-intl/server";

/**
 * Protected layout: requires JWT cookie (`pm_session`). If absent → redirect to /login.
 * The actual JWT validation happens server-side in FastAPI on every API request;
 * this is just a UX shortcut.
 */
export default async function DashboardLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  const t = await getTranslations({ locale, namespace: "common" });
  const cookieStore = await cookies();
  const session = cookieStore.get("pm_session");
  if (!session) {
    redirect("/login");
  }

  return (
    <div className="flex min-h-screen">
      <a
        href="#main-content"
        className="sr-only fixed left-3 top-3 z-[100] rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground focus:not-sr-only"
      >
        {t("skip_to_content")}
      </a>
      <SideNav />
      <MobileHeader />
      <main id="main-content" className="min-w-0 flex-1 pb-16 pt-14 md:pb-0 md:pt-0" tabIndex={-1}>
        <div className="container py-4 md:py-8">
          <NotificationsBanner />
          {children}
        </div>
      </main>
      <BottomNav />
    </div>
  );
}
