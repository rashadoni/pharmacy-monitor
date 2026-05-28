"""Streamlit auth gate через magic-link токены.

Use case:
- Дашборд защищён nginx basic-auth (один логин на всех)
- ДОПОЛНИТЕЛЬНО — каждый юзер логинится через magic-link для аудита кто что
  делает (Saved views / manual matches привязаны к user)

Flow:
1. Юзер открывает дашборд → если в session нет current_user, показывается логин-форма
2. Юзер вводит email → если в БД есть TenantUser с этим email, генерируется
   magic-token, отправляется по почте (notifier.send_email)
3. Юзер кликает ссылку из письма (formatted: ?magic_token=XXX)
4. Streamlit видит ?magic_token в query_params → verify_magic_token →
   сохраняет user в st.session_state.current_user

В скелете без email-отправки логин-формой "Я разработчик" токен можно ввести
вручную. На пилоте достаточно nginx basic-auth, magic-link это nice-to-have.
"""

from __future__ import annotations

import os

import streamlit as st

from src import storage, tenants


def is_authenticated() -> bool:
    return st.session_state.get("current_user") is not None


def get_current_user() -> dict | None:
    """Текущий аутентифицированный юзер из st.session_state."""
    return st.session_state.get("current_user")


def login_via_token(token: str) -> bool:
    """Проверить magic-token, сохранить user в session."""
    if not token:
        return False
    Session = storage.make_session()
    with Session() as s:
        user = tenants.verify_magic_token(s, token)
        if not user:
            return False
        st.session_state["current_user"] = {
            "id": user.id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        }
        return True


def logout() -> None:
    st.session_state.pop("current_user", None)


def render_login_gate() -> bool:
    """Если auth не включён или юзер уже залогинен — return True (можно рендерить).

    Иначе показывает логин-форму, return False (rendered, но не authenticated).
    """
    # Если auth выключен через env — пропускаем
    if os.environ.get("PHARMACY_AUTH_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return True

    # Magic-token из URL?
    qp = st.query_params
    token_in_url = qp.get("magic_token")
    if token_in_url and not is_authenticated():
        if isinstance(token_in_url, list):
            token_in_url = token_in_url[0]
        if login_via_token(token_in_url):
            # Очищаем query_params чтобы токен не оставался в адресной строке
            st.query_params.clear()
            st.rerun()

    if is_authenticated():
        return True

    # Render login form
    st.markdown(
        """
<div style="max-width:420px;margin:80px auto;text-align:center;">
  <div style="font-size:34px;">🔐</div>
  <h2 style="margin:8px 0 4px;">Вход в Pharmacy Monitor</h2>
  <div style="color:#86868b;font-size:14px;margin-bottom:24px;">
    Запросите magic-link на email — ссылка живёт 30 минут.
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        with st.form("login_form"):
            email = st.text_input("Email")
            submit = st.form_submit_button("📧 Запросить ссылку", type="primary")
            if submit:
                if not email or "@" not in email:
                    st.error("Введи валидный email")
                else:
                    Session = storage.make_session()
                    with Session() as s:
                        token = tenants.issue_magic_token(s, email)
                    if not token:
                        # Не подсказываем существует ли email (security)
                        st.success("Если такой email есть — ссылка отправлена.")
                    else:
                        # Дев-режим: показываем токен прямо
                        if os.environ.get("PHARMACY_AUTH_DEV_SHOW_TOKEN") == "1":
                            link = f"?magic_token={token}"
                            st.success(
                                f"DEV: открой [эту ссылку]({link}) "
                                f"или скопируй токен:\n```\n{token}\n```"
                            )
                        else:
                            # Прод: отправляем email через notifier
                            try:
                                from src import notifier

                                domain = os.environ.get(
                                    "PHARMACY_PUBLIC_URL", "http://localhost:8501"
                                )
                                link = f"{domain}/?magic_token={token}"
                                notifier.send_email(
                                    subject="Pharmacy Monitor — вход",
                                    html_body=(
                                        f"<p>Кликни на ссылку чтобы войти "
                                        f"(живёт 30 минут):</p>"
                                        f"<p><a href='{link}'>{link}</a></p>"
                                    ),
                                    to=[email],
                                )
                                st.success("Ссылка отправлена на email!")
                            except Exception as e:
                                st.error(f"Не удалось отправить email: {e}")

        with st.expander("Уже есть токен из письма?"):
            with st.form("token_form"):
                manual_token = st.text_input("Magic-token")
                if st.form_submit_button("Войти"):
                    if login_via_token(manual_token):
                        st.rerun()
                    else:
                        st.error("Неверный или просроченный токен")

    return False


def render_user_badge() -> None:
    """В сайдбаре — кто залогинен + кнопка выйти."""
    user = get_current_user()
    if not user:
        return
    st.sidebar.markdown(
        f"""
<div style="padding:10px;background:#fafafa;border-radius:8px;font-size:12px;">
  <div style="color:#86868b;">Вы вошли как</div>
  <div style="font-weight:600;color:#1d1d1f;">{user["name"] or user["email"]}</div>
  <div style="color:#86868b;font-size:11px;">{user["role"]}</div>
</div>
""",
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Выйти", key="logout_btn"):
        logout()
        st.rerun()
