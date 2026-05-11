import { useEffect, useState } from "react";

/**
 * Debounce a value. Useful for search inputs to avoid spamming the API on every keystroke.
 *
 *   const [search, setSearch] = useState("");
 *   const debouncedSearch = useDebounce(search, 300);
 *   useQuery({ queryKey: ["x", debouncedSearch], ... });
 */
export function useDebounce<T>(value: T, delayMs: number = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}
