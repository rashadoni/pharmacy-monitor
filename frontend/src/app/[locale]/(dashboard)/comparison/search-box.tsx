"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { useDebounce } from "@/lib/use-debounce";

// С одной буквы поиск вернул бы пол-каталога — ждём вторую.
export const MIN_SEARCH_CHARS = 2;

/**
 * Поле поиска с подсказками при наборе — «как в Google»: предлагает торговые
 * имена из каталога (и исправляет опечатки), выбор подставляет имя в поиск.
 */
export function SearchBox({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  const t = useTranslations("comparison");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const term = useDebounce(value.trim(), 120);
  const enabled = term.length >= MIN_SEARCH_CHARS;
  const { data } = useQuery({
    queryKey: ["comparison-suggest", term],
    queryFn: () => api.comparisonSuggest(term),
    enabled,
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  });
  // Единственную подсказку, равную набранному, не показываем — она ничего не даёт.
  const suggestions = useMemo(() => {
    if (!enabled || !data) return [];
    const typed = value.trim().toLowerCase();
    return data.length === 1 && data[0].text.toLowerCase() === typed ? [] : data;
  }, [data, enabled, value]);
  const visible = open && suggestions.length > 0;
  useEffect(() => setActive(-1), [suggestions]);

  function pick(text: string) {
    onChange(text);
    setOpen(false);
  }

  return (
    <div className="relative flex-1 md:min-w-64">
      <input
        type="search"
        role="combobox"
        aria-expanded={visible}
        aria-controls="comparison-suggestions"
        aria-autocomplete="list"
        aria-activedescendant={
          visible && active >= 0 ? `comparison-suggestion-${active}` : undefined
        }
        autoComplete="off"
        maxLength={100}
        placeholder={t("search_placeholder")}
        aria-label={t("search_label")}
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            // У type="search" Escape по умолчанию стирает набранное. Первое
            // нажатие должно только закрыть подсказки.
            if (visible) e.preventDefault();
            setOpen(false);
            return;
          }
          if (!visible) return;
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setActive((i) => (i + 1) % suggestions.length);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((i) => (i <= 0 ? suggestions.length - 1 : i - 1));
          } else if (e.key === "Enter" && active >= 0) {
            e.preventDefault();
            pick(suggestions[active].text);
          }
        }}
        className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:min-h-9"
        data-testid="search-input"
      />
      {visible && (
        <ul
          id="comparison-suggestions"
          role="listbox"
          aria-label={t("suggest_label")}
          className="absolute left-0 right-0 top-full z-20 mt-1 overflow-hidden rounded-md border border-border bg-background shadow-lg"
          data-testid="search-suggestions"
        >
          {suggestions.map((s, i) => (
            <li
              key={s.text}
              id={`comparison-suggestion-${i}`}
              role="option"
              aria-selected={i === active}
              // mousedown, а не click: иначе поле успевает потерять фокус и
              // список закрывается раньше, чем сработает выбор.
              onMouseDown={(e) => {
                e.preventDefault();
                pick(s.text);
              }}
              onMouseEnter={() => setActive(i)}
              className={`flex min-h-11 cursor-pointer items-center justify-between gap-3 px-3 py-2 text-sm md:min-h-9 ${
                i === active ? "bg-muted" : ""
              }`}
            >
              <span className="truncate">{s.text}</span>
              <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                {s.count}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
