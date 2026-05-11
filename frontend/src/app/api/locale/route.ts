/**
 * POST /api/locale  { locale: "ru" | "az" | "en" }
 *
 * Sets the `pm_locale` cookie which next-intl reads on every request.
 * After 200 OK, the client should reload to re-render with new messages.
 */
import { NextResponse } from "next/server";
import { cookies } from "next/headers";

const SUPPORTED = new Set(["ru", "az", "en"]);

export async function POST(req: Request) {
  const body = await req.json().catch(() => ({}));
  const locale = (body?.locale ?? "").toString();
  if (!SUPPORTED.has(locale)) {
    return NextResponse.json({ error: "unsupported locale" }, { status: 400 });
  }
  const cookieStore = await cookies();
  cookieStore.set("pm_locale", locale, {
    maxAge: 60 * 60 * 24 * 365, // 1 year
    path: "/",
    sameSite: "lax",
    httpOnly: false,
  });
  return NextResponse.json({ ok: true, locale });
}
