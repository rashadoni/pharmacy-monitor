"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Lock, User } from "lucide-react";

export default function LoginPage() {
  const router = useRouter();
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setStatus("loading");
    setErrorMsg(null);
    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data?.detail || "Ошибка входа");
      }
      // Cookie set, redirect to overview
      router.push("/overview");
      router.refresh();
    } catch (err) {
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : "Ошибка");
    }
  }

  return (
    <div className="min-h-screen relative flex items-center justify-center px-4 overflow-hidden bg-gradient-to-br from-slate-50 via-white to-slate-100 dark:from-slate-950 dark:via-slate-900 dark:to-slate-950">
      {/* Subtle decorative gradient blobs */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden>
        <div className="absolute -top-40 -left-40 h-96 w-96 rounded-full bg-primary/10 blur-3xl" />
        <div className="absolute -bottom-40 -right-40 h-96 w-96 rounded-full bg-blue-400/10 blur-3xl" />
      </div>

      <div className="relative w-full max-w-md">
        {/* Card */}
        <div className="rounded-2xl border border-border bg-card/95 backdrop-blur-sm shadow-xl shadow-black/5 dark:shadow-black/20 p-8 md:p-10">
          {/* Logo / Brand */}
          <div className="mb-8 text-center">
            <div className="inline-flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 mb-4">
              <Lock className="h-7 w-7 text-primary" />
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">Pharmacy Monitor</h1>
            <p className="text-sm text-muted-foreground mt-1.5">
              Введите логин и пароль для входа
            </p>
          </div>

          <form onSubmit={onSubmit} className="space-y-4">
            {/* Login */}
            <div>
              <label htmlFor="login" className="text-sm font-medium block mb-1.5">
                Логин
              </label>
              <div className="relative">
                <User className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
                <input
                  id="login"
                  type="text"
                  required
                  autoFocus
                  autoComplete="username"
                  value={login}
                  onChange={(e) => setLogin(e.target.value)}
                  placeholder="admin"
                  disabled={status === "loading"}
                  className="w-full rounded-lg border border-input bg-background pl-9 pr-3 py-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-transparent transition-shadow"
                />
              </div>
            </div>

            {/* Password */}
            <div>
              <label htmlFor="password" className="text-sm font-medium block mb-1.5">
                Пароль
              </label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
                <input
                  id="password"
                  type="password"
                  required
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  disabled={status === "loading"}
                  className="w-full rounded-lg border border-input bg-background pl-9 pr-3 py-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-transparent transition-shadow"
                />
              </div>
            </div>

            {/* Error */}
            {errorMsg && (
              <div
                role="alert"
                className="rounded-lg bg-destructive/10 border border-destructive/30 px-3 py-2 text-sm text-destructive flex items-start gap-2"
              >
                <span className="leading-tight">{errorMsg}</span>
              </div>
            )}

            {/* Submit */}
            <button
              type="submit"
              disabled={status === "loading" || !login || !password}
              className="w-full rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-150 active:scale-[0.99] shadow-sm"
            >
              {status === "loading" ? "Входим…" : "Войти"}
            </button>
          </form>
        </div>

        {/* Footer */}
        <p className="text-center text-xs text-muted-foreground mt-6">
          Pharmacy Monitor · Production
        </p>
      </div>
    </div>
  );
}
