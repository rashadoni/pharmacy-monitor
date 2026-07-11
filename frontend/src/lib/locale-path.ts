export function pathWithSearch(pathname: string, search: string): string {
  const normalized = search.replace(/^\?/, "");
  return normalized ? `${pathname}?${normalized}` : pathname;
}
