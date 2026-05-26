/**
 * POST /api/locale  { locale: "ru" | "az" | "en" }
 *
 * Sets the `pm_locale` cookie which next-intl reads on every request.
 * After 200 OK, the client should reload to re-render with new messages.
 */
import { NextRequest, NextResponse } from "next/server";

const SUPPORTED = new Set(["ru", "az", "en"]);

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const locale = (body?.locale ?? "").toString();
  if (!SUPPORTED.has(locale)) {
    return NextResponse.json({ error: "unsupported locale" }, { status: 400 });
  }
  const response = NextResponse.json({ ok: true, locale });
  response.cookies.set("pm_locale", locale, {
    maxAge: 60 * 60 * 24 * 365, // 1 year
    path: "/",
    sameSite: "lax",
    httpOnly: false,
  });
  return response;
}
