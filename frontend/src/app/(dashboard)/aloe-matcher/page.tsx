import { redirect } from "next/navigation";

/**
 * Backwards-compat redirect: /aloe-matcher → /matcher?site=aloe.
 *
 * Старый раздел был узко-специализирован под aloe. Универсальный матчер
 * (/matcher) поддерживает все 3 сайта + режим create-from-scratch. Этот файл
 * сохраняет старые bookmarks работающими.
 */
export default function AloeMatcherRedirect() {
  redirect("/matcher?site=aloe&mode=attach");
}
