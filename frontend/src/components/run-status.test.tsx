// @vitest-environment jsdom

import React from "react";
import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";

import az from "../../messages/az.json";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";
import type { LatestRunBySite, RunRow } from "../lib/api";
import { IsolatedIdentitiesNote, LatestSiteRunStatus } from "./run-status";

type Messages = React.ComponentProps<typeof NextIntlClientProvider>["messages"];

function run(id: number, status: string, isolated: number | null = null): RunRow {
  return {
    id,
    started_at: "2026-10-30T01:00:00",
    finished_at: "2026-10-30T01:05:00",
    status,
    products_scraped: 9443,
    products_per_site: { pharmonline: 9443 },
    isolated_identities:
      isolated == null
        ? null
        : { site: "pharmonline", trusted_count: 9443 - isolated, isolated_count: isolated },
    run_quality: null,
    sites_completed: "pharmonline",
    error_message: null,
  };
}

function shown(locale: string, messages: Messages, element: React.ReactElement) {
  const { container } = render(
    <NextIntlClientProvider locale={locale} messages={messages}>
      {element}
    </NextIntlClientProvider>,
  );
  const root = container.firstElementChild as HTMLElement | null;
  return {
    // Разделитель разрядов — неразрывный пробел; сравниваем с обычным.
    lines: [...(root?.children ?? [])].map((el) =>
      (el.textContent ?? "").replace(/\s/g, " "),
    ),
    titles: [...(root?.querySelectorAll("[title]") ?? [])].map((el) =>
      el.getAttribute("title"),
    ),
  };
}

function site(item: Partial<LatestRunBySite>): LatestRunBySite {
  return { site: "pharmonline", run: null, latest_attempt: null, ...item };
}

describe("LatestSiteRunStatus", () => {
  it("shows a run with isolated cards as the latest good run, not as a newer problem", () => {
    // С 16.10 так кончается каждый ночной сбор pharmonline: 4 карточки изолированы.
    const partitioned = run(1160, "degraded", 4);
    const item = site({ run: partitioned, latest_attempt: partitioned });

    expect(shown("ru", ru, <LatestSiteRunStatus item={item} />).lines).toEqual([
      "Неполный",
      "4 карточки изолированы, остальные 9 439 допущены к расчётам",
    ]);
    expect(shown("az", az, <LatestSiteRunStatus item={item} />).lines).toEqual([
      "Natamam",
      "4 məhsul kartı təcrid edilib, qalan 9.439 hesablamalara buraxılıb",
    ]);
    expect(shown("en", en, <LatestSiteRunStatus item={item} />).lines).toEqual([
      "Incomplete",
      "4 cards isolated; the other 9,439 are admitted to calculations",
    ]);
    expect(shown("ru", ru, <LatestSiteRunStatus item={item} />).titles).toEqual([
      ru.overview.isolated_identities_hint,
    ]);
  });

  it("still warns about a newer attempt that really failed", () => {
    // Пометка говорит о самом прогоне («допущены»), а не о том, что цены сейчас
    // в расчётах: после упавшей попытки эпоха доверия закрыта.
    const item = site({ run: run(1160, "degraded", 4), latest_attempt: run(1161, "failed") });

    expect(shown("ru", ru, <LatestSiteRunStatus item={item} />).lines).toEqual([
      "Неполный",
      "4 карточки изолированы, остальные 9 439 допущены к расчётам",
      "новая попытка #1161 завершилась с проблемой",
    ]);
  });

  it("adds nothing to a plain successful run", () => {
    const ok = run(1034, "ok");
    expect(
      shown("ru", ru, <LatestSiteRunStatus item={site({ run: ok, latest_attempt: ok })} />).lines,
    ).toEqual(["Успешно"]);
  });

  it("shows a dash and the failure when the site has no good run", () => {
    const item = site({ latest_attempt: run(1161, "failed") });
    expect(shown("en", en, <LatestSiteRunStatus item={item} />).lines).toEqual([
      "—",
      "new attempt #1161 finished with a problem",
    ]);
  });
});

describe("IsolatedIdentitiesNote", () => {
  const note = (isolated: number) =>
    shown(
      "ru",
      ru,
      <div>
        <IsolatedIdentitiesNote
          isolated={{ site: "pharmonline", trusted_count: 9000, isolated_count: isolated }}
        />
      </div>,
    ).lines[0];

  it("keeps Russian plural forms", () => {
    expect(note(1)).toBe("1 карточка изолирована, остальные 9 000 допущены к расчётам");
    expect(note(21)).toBe("21 карточка изолирована, остальные 9 000 допущены к расчётам");
    expect(note(5)).toBe("5 карточек изолировано, остальные 9 000 допущены к расчётам");
    expect(note(22)).toBe("22 карточки изолированы, остальные 9 000 допущены к расчётам");
  });

  it("renders nothing for a run without isolated cards", () => {
    const { lines } = shown("ru", ru, <div><IsolatedIdentitiesNote isolated={null} /></div>);
    expect(lines).toEqual([]);
  });
});
