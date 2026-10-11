/**
 * Правило пароля — то же, что на сервере (`src/api.py`, `_password_problem`).
 *
 * bcrypt берёт от пароля 72 байта UTF-8: латинский знак — один байт,
 * кириллический и буквы вроде ə, ö, ş — два. Сервер на более длинном отвечает
 * отказом (422, причина в `detail[].type`); здесь то же правило проверяется до
 * отправки, чтобы человек увидел причину на своём языке и сразу.
 *
 * Пробелы по краям не считаются: сервер их отбрасывает — и когда пароль задают,
 * и когда им входят. Что считать пробелом, `trim()` и сервер на редких знаках
 * решают по-разному; последнее слово — за ответом сервера.
 */
export const PASSWORD_MIN_CHARS = 6;
export const PASSWORD_MAX_BYTES = 72;

export type PasswordProblem =
  | "password_not_text"
  | "password_too_short"
  | "password_too_long";

const PROBLEMS: readonly string[] = [
  "password_not_text",
  "password_too_short",
  "password_too_long",
];

/** Почему строку нельзя задать паролем; можно — `null`. */
export function passwordProblem(raw: string): PasswordProblem | null {
  const password = raw.trim();
  let chars = 0;
  for (const char of password) {
    const code = char.codePointAt(0) ?? 0;
    // Половина суррогатной пары: в UTF-8 такой знак не записать. Нулевой знак
    // bcrypt берёт за конец пароля.
    if (code === 0 || (code >= 0xd800 && code <= 0xdfff)) {
      return "password_not_text";
    }
    chars += 1;
  }
  if (chars < PASSWORD_MIN_CHARS) return "password_too_short";
  if (new TextEncoder().encode(password).length > PASSWORD_MAX_BYTES) {
    return "password_too_long";
  }
  return null;
}

/** Причина отказа из тела ответа сервера, если отказ — по правилу пароля. */
export function passwordProblemInResponse(body: unknown): PasswordProblem | null {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (!Array.isArray(detail)) return null;
  for (const item of detail) {
    const type = (item as { type?: unknown } | null)?.type;
    if (typeof type === "string" && PROBLEMS.includes(type)) {
      return type as PasswordProblem;
    }
  }
  return null;
}
