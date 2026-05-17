import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { SideNav, BottomNav } from "@/components/nav";

/**
 * Protected layout: requires JWT cookie (`pm_session`). If absent → redirect to /login.
 * The actual JWT validation happens server-side in FastAPI on every API request;
 * this is just a UX shortcut.
 */
export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const cookieStore = await cookies();
  const session = cookieStore.get("pm_session");
  if (!session) {
    redirect("/login");
  }

  return (
    <div className="flex min-h-screen">
      {/* a11y: skip-to-main для screen reader / keyboard пользователей */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-primary-foreground focus:shadow-lg"
      >
        Перейти к основному контенту
      </a>
      <SideNav />
      <main id="main-content" className="flex-1 pb-16 md:pb-0" tabIndex={-1}>
        <div className="container py-4 md:py-8">{children}</div>
      </main>
      <BottomNav />
    </div>
  );
}
