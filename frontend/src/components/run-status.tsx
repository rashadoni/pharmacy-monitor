"use client";

import React from "react";
import { useTranslations } from "next-intl";

import type { IsolatedIdentities, LatestRunBySite } from "../lib/api";
import { newerProblemAttempt, runStatusToneClass } from "../lib/run-quality";

export function RunStatusBadge({ status }: { status: string }) {
  const t = useTranslations("overview");
  const translated = ["ok", "running", "degraded", "failed"].includes(status)
    ? t(`status_${status}` as "status_ok")
    : status;
  return (
    <span
      className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${runStatusToneClass(status)}`}
    >
      {translated}
    </span>
  );
}

/**
 * «Неполный» у полного сбора, где сайт пересоздал несколько карточек, без
 * объяснения читается как «данные негодные». Пометка говорит, сколько карточек
 * ждёт сверки и что цены остальных уже в расчётах.
 */
export function IsolatedIdentitiesNote({
  isolated,
}: {
  isolated: IsolatedIdentities | null | undefined;
}) {
  const t = useTranslations("overview");
  if (!isolated) return null;
  return (
    <span className="text-xs text-muted-foreground" title={t("isolated_identities_hint")}>
      {t("isolated_identities", {
        isolated: isolated.isolated_count,
        trusted: isolated.trusted_count,
      })}
    </span>
  );
}

/** Состояние сайта в таблице «Последний прогон по каждому сайту». */
export function LatestSiteRunStatus({ item }: { item: LatestRunBySite }) {
  const t = useTranslations("overview");
  const run = item.run;
  const problem = newerProblemAttempt(item);
  return (
    <div className="flex flex-col items-start gap-1">
      {run ? (
        <RunStatusBadge status={run.status} />
      ) : (
        <span className="text-muted-foreground">—</span>
      )}
      <IsolatedIdentitiesNote isolated={run?.isolated_identities} />
      {problem && (
        <span className="text-xs text-warning">
          {t("newer_attempt_warning", { id: problem.id })}
        </span>
      )}
    </div>
  );
}
