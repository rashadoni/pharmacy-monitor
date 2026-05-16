import { redirect } from "next/navigation";

/**
 * Backwards-compat: /aloe was the original URL before symmetric /site/[name].
 * Server-side redirect to the new dynamic route. Old browser bookmarks keep working.
 */
export default function AloeLegacyRedirect() {
  redirect("/site/aloe");
}
