"""Streamlit-дашборд для просмотра данных мониторинга в браузере.

Запуск:
    uv run streamlit run src/dashboard.py

Содержит 3 вкладки:
- 📊 Обзор    — KPI, undercuts, тренды, gap-анализ, промо
- 📋 Watchlist — CRUD конкретных отслеживаемых товаров
- 📧 Получатели — CRUD email-получателей
"""

from __future__ import annotations

import os
import subprocess
from datetime import timedelta
from src._time import utcnow
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine

from src import auth, storage, watchlist as wl
from src.url_parser import parse_urls_block

load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parent.parent

st.set_page_config(
    page_title="Pharmacy Monitor",
    page_icon="💊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# === Custom styling ===
st.markdown(
    """
<style>
/* System font stack — Apple-like clean typography */
html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI",
                 Roboto, Oxygen-Sans, Ubuntu, Cantarell, "Helvetica Neue", sans-serif !important;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}

/* Главный контейнер — меньше воздуха сверху */
.main .block-container {
    padding-top: 2.2rem;
    padding-bottom: 3rem;
    max-width: 1280px;
}

/* Заголовок и подзаголовок */
h1 {
    font-weight: 600 !important;
    letter-spacing: -0.02em;
    color: #1d1d1f;
}
h2, h3, h4 {
    font-weight: 600 !important;
    letter-spacing: -0.01em;
    color: #1d1d1f;
}
.stCaption, [data-testid="stCaptionContainer"] {
    color: #6e6e73 !important;
}

/* Tabs — крупнее и чище */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    background: #f5f5f7;
    padding: 4px;
    border-radius: 10px;
    border-bottom: none;
}
.stTabs [data-baseweb="tab"] {
    height: 38px;
    padding: 0 16px;
    background: transparent;
    border-radius: 7px;
    font-weight: 500;
    color: #6e6e73;
    transition: all 150ms ease;
}
.stTabs [data-baseweb="tab"]:hover {
    color: #1d1d1f;
}
.stTabs [aria-selected="true"] {
    background: #ffffff !important;
    color: #1d1d1f !important;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
}
.stTabs [data-baseweb="tab-panel"] {
    padding-top: 22px;
}

/* Метрики — карточки с тонкой границей */
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #e5e5e7;
    border-radius: 10px;
    padding: 14px 16px;
    box-shadow: 0 1px 1px rgba(0, 0, 0, 0.02);
}
[data-testid="stMetricLabel"] {
    font-size: 11px !important;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #6e6e73 !important;
    font-weight: 500;
}
[data-testid="stMetricValue"] {
    font-size: 28px !important;
    font-weight: 600 !important;
    letter-spacing: -0.02em;
    color: #1d1d1f !important;
}

/* Кнопки — чище и крупнее */
.stButton > button {
    border-radius: 8px;
    font-weight: 500;
    transition: all 120ms ease;
    border: 1px solid #d2d2d7;
    box-shadow: none;
}
.stButton > button:hover {
    transform: translateY(-1px);
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.08);
}
.stButton > button[kind="primary"] {
    background: #0066cc;
    border-color: #0066cc;
}
.stButton > button[kind="primary"]:hover {
    background: #0055b3;
    border-color: #0055b3;
}

/* Expanders — мягче */
.streamlit-expanderHeader, [data-testid="stExpander"] summary {
    border-radius: 10px;
    background: #ffffff;
    border: 1px solid #e5e5e7 !important;
    font-weight: 500;
    transition: background 120ms;
}
.streamlit-expanderHeader:hover {
    background: #fafafa;
}
[data-testid="stExpander"] {
    border: none !important;
    box-shadow: none !important;
}

/* Контейнеры с border (st.container(border=True)) */
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 12px !important;
    border: 1px solid #e5e5e7 !important;
    background: #ffffff;
    padding: 6px !important;
}

/* Таблицы — мягче */
[data-testid="stDataFrame"] {
    border-radius: 8px;
    overflow: hidden;
}

/* Sidebar — чище */
[data-testid="stSidebar"] {
    background: #fafafa;
    border-right: 1px solid #e5e5e7;
}
[data-testid="stSidebar"] .stButton > button {
    background: #ffffff;
}

/* Inputs */
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stSelectbox > div > div {
    border-radius: 8px !important;
    border: 1px solid #d2d2d7 !important;
    transition: border-color 120ms;
}
.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus {
    border-color: #0066cc !important;
    box-shadow: 0 0 0 3px rgba(0, 102, 204, 0.1);
}

/* Info / success / warning / error блоки — softer */
[data-testid="stAlert"] {
    border-radius: 10px;
    border: none;
    padding: 14px 18px;
}

/* Дивайдеры более тонкие */
hr {
    margin: 1.2rem 0 !important;
    border-color: #e5e5e7 !important;
}

/* Убрать лишний хедер */
header[data-testid="stHeader"] {
    background: transparent;
    height: 0;
}

/* Убрать всю верхнюю панель Streamlit (Deploy / Manage app / меню) */
footer,
.stDeployButton,
[data-testid="stDeployButton"],
[data-testid="stToolbar"],
[data-testid="stToolbarActions"],
[data-testid="stStatusWidget"],
[data-testid="stMainMenu"],
button[kind="header"],
.stAppDeployButton,
header [data-testid="stMainMenuPopover"] {
    display: none !important;
    visibility: hidden !important;
}

/* Спрячем header полностью — он только пустое пространство держит */
header[data-testid="stHeader"] {
    display: none !important;
}

/* Убрать "running" / "stop" индикаторы в углу */
[data-testid="stStatusIndicator"],
[data-testid="stStatus"] {
    display: none !important;
}

/* Caption под секцией */
.section-caption {
    color: #6e6e73;
    font-size: 13px;
    margin-top: -8px;
    margin-bottom: 12px;
}

/* === MOBILE-RESPONSIVE — viewport <760px === */
@media (max-width: 760px) {
    .main .block-container {
        padding-top: 1rem;
        padding-left: 0.6rem;
        padding-right: 0.6rem;
    }
    h1 { font-size: 22px !important; }
    h2 { font-size: 18px !important; }
    h3 { font-size: 16px !important; }
    /* Метрики — компактнее */
    [data-testid="stMetric"] { padding: 8px 10px; }
    [data-testid="stMetricValue"] { font-size: 20px !important; }
    [data-testid="stMetricLabel"] { font-size: 10px !important; }
    /* Tabs — горизонтальный скролл */
    .stTabs [data-baseweb="tab-list"] {
        overflow-x: auto;
        flex-wrap: nowrap;
        scrollbar-width: thin;
    }
    .stTabs [data-baseweb="tab"] {
        flex-shrink: 0;
        font-size: 13px;
        padding: 0 10px;
    }
    /* Карточки сравнения — короче подписи */
    .streamlit-expanderHeader {
        font-size: 13px;
    }
    /* Sidebar при мобильном — выезжает поверх */
    [data-testid="stSidebar"] {
        width: 80vw !important;
    }
    /* Таблицы — горизонтальный скролл */
    [data-testid="stDataFrame"] {
        overflow-x: auto;
    }
}
</style>
""",
    unsafe_allow_html=True,
)

CLIENT_SITE = "pharmonline"
COMPETITOR_SITES = ("aptekonline", "aloe")
SITES = (CLIENT_SITE, *COMPETITOR_SITES)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/db.sqlite")

storage.init_db()

# Auth gate (включается через PHARMACY_AUTH_ENABLED=1 в .env)
if not auth.render_login_gate():
    st.stop()
auth.render_user_badge()


@st.cache_resource
def get_engine():
    return create_engine(DATABASE_URL)


def get_session():
    Session = storage.make_session()
    return Session()


@st.cache_data(ttl=30)
def load_runs() -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, started_at, finished_at, status, products_scraped FROM runs ORDER BY started_at DESC",
        get_engine(),
        parse_dates=["started_at", "finished_at"],
    )


@st.cache_data(ttl=30)
def load_latest_snapshot() -> pd.DataFrame:
    query = """
    SELECT
        p.id AS product_id, p.site, p.name, p.brand, p.category, p.url, p.canonical_id,
        ps.price, ps.discount_price, ps.discount_percent, ps.is_on_sale, ps.promo_label,
        r.id AS run_id, r.started_at AS run_started_at
    FROM price_snapshots ps
    JOIN products p ON p.id = ps.product_id
    JOIN runs r ON r.id = ps.run_id
    WHERE r.id = (SELECT MAX(id) FROM runs WHERE status = 'ok')
    """
    return pd.read_sql(query, get_engine(), parse_dates=["run_started_at"])


@st.cache_data(ttl=30)
def load_price_history() -> pd.DataFrame:
    query = """
    SELECT
        p.site, p.name, p.brand, p.canonical_id, p.id AS product_id,
        COALESCE(ps.discount_price, ps.price) AS price,
        ps.is_on_sale, r.started_at AS run_started_at
    FROM price_snapshots ps
    JOIN products p ON p.id = ps.product_id
    JOIN runs r ON r.id = ps.run_id
    WHERE r.status = 'ok'
    ORDER BY r.started_at
    """
    return pd.read_sql(query, get_engine(), parse_dates=["run_started_at"])


@st.cache_data(ttl=30)
def load_matches() -> pd.DataFrame:
    query = """
    SELECT m.id AS canonical_id, m.canonical_name, m.is_manual,
           p.site, p.name, p.url, p.id AS product_id
    FROM matches m
    JOIN products p ON p.canonical_id = m.id
    """
    return pd.read_sql(query, get_engine())


@st.cache_data(ttl=30)
def load_promos() -> pd.DataFrame:
    return pd.read_sql(
        "SELECT site, title, landing_url, captured_at, run_id FROM promos ORDER BY captured_at DESC",
        get_engine(),
        parse_dates=["captured_at"],
    )


@st.cache_data(ttl=30)
def load_comparison() -> pd.DataFrame:
    """Cross-site сравнение: для каждого Match — цена на каждом сайте.

    Возвращает DataFrame с одной строкой на canonical_id.
    """
    query = """
    WITH latest AS (
        SELECT MAX(id) AS id FROM runs WHERE status = 'ok'
    ),
    snap AS (
        SELECT
            p.id AS product_id, p.canonical_id, p.site, p.name, p.brand, p.url, p.image_url,
            COALESCE(ps.discount_price, ps.price) AS effective_price,
            ps.is_on_sale, ps.discount_percent, ps.promo_label
        FROM products p
        LEFT JOIN price_snapshots ps ON ps.product_id = p.id
            AND ps.run_id = (SELECT id FROM latest)
        WHERE p.canonical_id IS NOT NULL
    ),
    matched AS (
        SELECT
            m.id AS canonical_id, m.canonical_name, m.canonical_brand,
            m.canonical_dosage, m.canonical_pack_size,
            MAX(CASE WHEN s.site='pharmonline' THEN s.effective_price END) AS ph_price,
            MAX(CASE WHEN s.site='pharmonline' THEN s.url END)             AS ph_url,
            MAX(CASE WHEN s.site='pharmonline' THEN s.is_on_sale END)      AS ph_sale,
            MAX(CASE WHEN s.site='pharmonline' THEN s.image_url END)       AS ph_img,
            MAX(CASE WHEN s.site='pharmonline' THEN s.product_id END)      AS ph_pid,
            MAX(CASE WHEN s.site='aptekonline' THEN s.effective_price END) AS ap_price,
            MAX(CASE WHEN s.site='aptekonline' THEN s.url END)             AS ap_url,
            MAX(CASE WHEN s.site='aptekonline' THEN s.is_on_sale END)      AS ap_sale,
            MAX(CASE WHEN s.site='aptekonline' THEN s.image_url END)       AS ap_img,
            MAX(CASE WHEN s.site='aptekonline' THEN s.product_id END)      AS ap_pid,
            MAX(CASE WHEN s.site='aloe'        THEN s.effective_price END) AS al_price,
            MAX(CASE WHEN s.site='aloe'        THEN s.url END)             AS al_url,
            MAX(CASE WHEN s.site='aloe'        THEN s.is_on_sale END)      AS al_sale,
            MAX(CASE WHEN s.site='aloe'        THEN s.image_url END)       AS al_img,
            MAX(CASE WHEN s.site='aloe'        THEN s.product_id END)      AS al_pid,
            MAX(m.is_manual) AS is_manual
        FROM matches m
        LEFT JOIN snap s ON s.canonical_id = m.id
        GROUP BY m.id
    )
    SELECT * FROM matched
    """
    df = pd.read_sql(query, get_engine())
    if df.empty:
        return df
    # cheapest_site и spread
    price_cols = ["ph_price", "ap_price", "al_price"]
    df["min_price"] = df[price_cols].min(axis=1)
    df["max_price"] = df[price_cols].max(axis=1)
    df["spread_pct"] = (
        (df["max_price"] - df["min_price"]) / df["max_price"] * 100
    ).round(1)
    df["sites_with_price"] = df[price_cols].notna().sum(axis=1)

    def _cheapest(row):
        prices = {
            "pharmonline": row["ph_price"],
            "aptekonline": row["ap_price"],
            "aloe": row["al_price"],
        }
        prices = {k: v for k, v in prices.items() if pd.notna(v)}
        if not prices:
            return None
        return min(prices, key=prices.get)

    df["cheapest_site"] = df.apply(_cheapest, axis=1)
    return df


def reset_caches() -> None:
    st.cache_data.clear()


def run_pharmacy_monitor(
    *,
    dry_run: bool = True,
    sites: list[str] | None = None,
    mode: str = "auto",
) -> tuple[int, str]:
    """Запустить `pharmacy-monitor run` через subprocess. Возвращает (returncode, log)."""
    args = ["uv", "run", "pharmacy-monitor", "run", "--mode", mode]
    if dry_run:
        args.append("--dry-run")
    for s in sites or []:
        args.extend(["--site", s])
    env = os.environ.copy()
    env.setdefault("SCRAPE_HEADLESS", "true")
    proc = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def render_run_panel(*, key_prefix: str, default_dry_run: bool = True) -> None:
    """UI-блок для запуска прогона: чекбоксы + кнопка + лог + автообновление."""
    col_opts, col_btn = st.columns([3, 1])
    with col_opts:
        oc1, oc2 = st.columns(2)
        with oc1:
            dry = st.checkbox(
                "Без отправки email",
                value=default_dry_run,
                help="При первом запуске оставь включённым — отчёт ляжет в reports/",
                key=f"{key_prefix}_dry",
            )
        with oc2:
            sites_chosen = st.multiselect(
                "Сайты (пусто = все)",
                options=list(SITES),
                default=[],
                key=f"{key_prefix}_sites",
            )
    with col_btn:
        st.markdown("&nbsp;")  # выровнять
        clicked = st.button(
            "▶️ Запустить прогон",
            type="primary",
            use_container_width=True,
            key=f"{key_prefix}_btn",
        )
    if clicked:
        with st.spinner(
            "🔄 Запускаю scraping... обычно 30-90 сек, не закрывай вкладку"
        ):
            try:
                rc, log = run_pharmacy_monitor(
                    dry_run=dry, sites=sites_chosen or None, mode="auto"
                )
            except subprocess.TimeoutExpired:
                rc, log = -1, "⛔ Превышен timeout 15 мин — прогон убит."
        if rc == 0:
            reset_caches()
            st.success("✅ Прогон завершён. Данные обновлены.")
            with st.expander("📜 Лог прогона", expanded=False):
                st.code(log[-5000:], language="bash")
            st.rerun()
        else:
            st.error(f"❌ Прогон упал (exit {rc})")
            st.code(log[-5000:], language="bash")


# === LAYOUT ===

st.markdown(
    """
<div style="display:flex;align-items:center;gap:14px;margin-bottom:6px;">
  <div style="font-size:34px;line-height:1;">💊</div>
  <div>
    <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#6e6e73;font-weight:500;">
      Pharmacy Monitor
    </div>
    <h1 style="margin:2px 0 0;font-size:28px;font-weight:600;letter-spacing:-.02em;">
      Мониторинг конкурентов
    </h1>
  </div>
</div>
<div style="color:#6e6e73;font-size:14px;margin-bottom:18px;">
  pharmonline.az  ·  aptekonline.az  ·  aloe.az
</div>
""",
    unsafe_allow_html=True,
)

# Sidebar — utility-кнопки + tenant switcher (multi-tenant scope).
with st.sidebar:
    from src import tenants as _t_mod

    with get_session() as _t_s:
        _tenants = _t_mod.list_tenants(_t_s, active_only=False)

    if len(_tenants) > 1:
        # Показываем выбор только если >1 тенанта (агентский режим)
        cur_id = st.session_state.get("current_tenant_id", _t_mod.DEFAULT_TENANT_ID)
        options = {t.id: f"{t.slug} ({t.name})" for t in _tenants}
        chosen = st.selectbox(
            "🏢 Тенант",
            options=list(options.keys()),
            format_func=lambda x: options.get(x, str(x)),
            index=list(options.keys()).index(cur_id) if cur_id in options else 0,
        )
        if chosen != cur_id:
            st.session_state["current_tenant_id"] = chosen
            reset_caches()
            st.rerun()
        st.caption(f"Активный тенант: `{[t for t in _tenants if t.id == chosen][0].slug}`")
        st.divider()
    elif _tenants:
        st.caption(f"🏢 Тенант: `{_tenants[0].slug}`")
        st.divider()

    if st.button("🔄 Обновить данные", use_container_width=True):
        reset_caches()
        st.rerun()
    st.caption("Кэш дашборда — 30 сек")
    st.divider()

_SEVERITY_STYLES = {
    "critical":    {"bg": "#ffeded", "border": "#ff3b30", "icon": "🔴", "label": "Критично"},
    "warning":     {"bg": "#fff5e6", "border": "#ff9500", "icon": "⚠️", "label": "Внимание"},
    "opportunity": {"bg": "#eaf6ec", "border": "#34c759", "icon": "💡", "label": "Возможность"},
    "info":        {"bg": "#eef4fb", "border": "#0066cc", "icon": "ℹ️", "label": "Инфо"},
}

_TYPE_LABELS = {
    "price_raise":     "💰 Подними цену",
    "undercut":        "🔴 Конкурент дешевле",
    "assortment_gap":  "🆕 Нет в ассортименте",
    "promo_response":  "🎯 Промо конкурента",
}


def _render_action_card(a):
    """Карточка одного actionable пункта на главной."""
    style = _SEVERITY_STYLES.get(a.severity, _SEVERITY_STYLES["info"])
    impact = a.estimated_monthly_impact_azn
    if impact > 0:
        impact_text = f"+{impact:.0f} ₼/мес"
        impact_color = "#34c759"
    elif impact < 0:
        impact_text = f"{impact:.0f} ₼/мес"
        impact_color = "#ff3b30"
    else:
        impact_text = "—"
        impact_color = "#86868b"

    type_label = _TYPE_LABELS.get(a.type, a.type)

    # Цены: текущая → целевая
    if a.current_value_azn is not None and a.target_value_azn is not None:
        prices_html = (
            f"<span style='font-size:13px;color:#86868b;'>"
            f"{a.current_value_azn:.2f} → "
            f"<span style='color:#1d1d1f;font-weight:600;'>{a.target_value_azn:.2f} ₼</span>"
            f"</span>"
        )
    elif a.current_value_azn is not None:
        prices_html = (
            f"<span style='font-size:13px;color:#86868b;'>"
            f"{a.current_value_azn:.2f} ₼</span>"
        )
    else:
        prices_html = ""

    # Ссылки
    links_parts = []
    if a.product_url:
        links_parts.append(
            f"<a href='{a.product_url}' target='_blank' "
            f"style='font-size:12px;color:#0066cc;text-decoration:none;margin-right:12px;'>"
            f"→ Открыть товар (клиент)</a>"
        )
    if a.competitor_url:
        comp_label = a.competitor_site or "конкурент"
        links_parts.append(
            f"<a href='{a.competitor_url}' target='_blank' "
            f"style='font-size:12px;color:#0066cc;text-decoration:none;margin-right:12px;'>"
            f"→ {comp_label}</a>"
        )

    st.markdown(
        f"""
<div style="background:{style['bg']};border-left:4px solid {style['border']};
            border-radius:10px;padding:14px 18px;margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;">
    <div style="flex:1;">
      <div style="font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:#86868b;
                  font-weight:500;margin-bottom:4px;">
        {style['icon']} {style['label']}  ·  {type_label}
      </div>
      <div style="font-size:15px;font-weight:600;color:#1d1d1f;line-height:1.3;">
        {a.title}
      </div>
      <div style="font-size:13px;color:#3a3a3c;margin-top:6px;line-height:1.4;">
        {a.detail}
      </div>
      <div style="margin-top:8px;">{prices_html}</div>
      <div style="margin-top:8px;">{''.join(links_parts)}</div>
    </div>
    <div style="text-align:right;min-width:120px;">
      <div style="font-size:11px;color:#86868b;text-transform:uppercase;letter-spacing:.05em;">
        Эффект
      </div>
      <div style="font-size:22px;font-weight:600;color:{impact_color};line-height:1.1;">
        {impact_text}
      </div>
    </div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _format_price_cell(price, url, is_sale, is_cheapest, is_most_expensive):
    """HTML-строка для крупного ценника с бейджами."""
    if pd.isna(price) or price is None:
        return "<div style='color:#86868b;font-size:14px;padding:10px 0;'>не найден</div>"
    if is_cheapest:
        color = "#fff"
        bg = "#34c759"
        accent = "rgba(52, 199, 89, 0.15)"
        ring = "0 0 0 2px rgba(52, 199, 89, 0.25)"
    elif is_most_expensive:
        color = "#fff"
        bg = "#ff3b30"
        accent = "rgba(255, 59, 48, 0.10)"
        ring = "none"
    else:
        color = "#1d1d1f"
        bg = "transparent"
        accent = "transparent"
        ring = "none"
    sale_badge = (
        "<span style='background:rgba(255,149,0,0.14);color:#b85d00;font-size:10px;"
        "padding:2px 6px;border-radius:4px;font-weight:600;letter-spacing:.04em;margin-left:6px;'>"
        "🔥 SALE</span>"
    ) if is_sale else ""
    cheap_badge = (
        "<span style='font-size:10px;background:rgba(255,255,255,.25);"
        "padding:2px 6px;border-radius:4px;letter-spacing:.06em;margin-left:6px;font-weight:700;'>"
        "🏆 ДЕШЕВЛЕ</span>"
    ) if is_cheapest else ""
    pill = (
        f"<a href='{url}' target='_blank' style='text-decoration:none;'>"
        f"<div style='display:inline-flex;align-items:center;background:{bg};color:{color};"
        f"padding:8px 14px;border-radius:10px;font-weight:600;font-size:18px;"
        f"box-shadow:{ring};'>"
        f"<span>{price:.2f} ₼</span>{cheap_badge}</div></a>"
        f"<div style='margin-top:6px;'>{sale_badge}</div>"
    )
    return pill


def _render_comparison_card(row):
    """Карточка одного товара с ценами на 3 сайтах."""
    name = row["canonical_name"]
    brand = row.get("canonical_brand") or ""
    dosage = row.get("canonical_dosage") or ""

    prices_summary = []
    for site_key, label in [("ph_price", "ph"), ("ap_price", "ap"), ("al_price", "al")]:
        p = row.get(site_key)
        if pd.notna(p):
            prices_summary.append(f"{label} {p:.2f}")
    spread = row.get("spread_pct")
    spread_str = f"  Δ {spread:.1f}%" if pd.notna(spread) and spread > 0 else ""
    title = name
    if brand:
        title += f"  ·  {brand}"
    if dosage:
        title += f"  ·  {dosage}"
    title += "      |   " + " · ".join(prices_summary) + spread_str

    with st.expander(title, expanded=False):
        img = row.get("ph_img") or row.get("ap_img") or row.get("al_img")
        col_img, col_prices = st.columns([1, 4])
        with col_img:
            if img:
                try:
                    st.image(img, width=140)
                except Exception:
                    pass
        with col_prices:
            cheapest = row.get("cheapest_site")
            prices = {
                "pharmonline": row.get("ph_price"),
                "aptekonline": row.get("ap_price"),
                "aloe": row.get("al_price"),
            }
            available = {k: v for k, v in prices.items() if pd.notna(v)}
            most_exp = max(available, key=available.get) if len(available) >= 2 else None

            cols = st.columns(3)
            for col, (site_key, prefix) in zip(
                cols,
                [("pharmonline", "ph"), ("aptekonline", "ap"), ("aloe", "al")],
            ):
                with col:
                    site_label = {
                        "pharmonline": "🏠 Клиент (pharmonline)",
                        "aptekonline": "aptekonline.az",
                        "aloe": "aloe.az",
                    }[site_key]
                    st.markdown(f"**{site_label}**")
                    price = row.get(f"{prefix}_price")
                    url = row.get(f"{prefix}_url")
                    is_sale = bool(row.get(f"{prefix}_sale"))
                    if pd.isna(price):
                        st.markdown(
                            "<div style='color:#999;padding:8px 0;'>не найден</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        is_cheap = cheapest == site_key and len(available) >= 2
                        is_exp = most_exp == site_key and len(available) >= 2 and not is_cheap
                        cell = _format_price_cell(price, url, is_sale, is_cheap, is_exp)
                        st.markdown(
                            f"<div style='padding:8px 0;font-size:18px;'>{cell}</div>",
                            unsafe_allow_html=True,
                        )
                        ph_price = row.get("ph_price")
                        if (
                            site_key != "pharmonline"
                            and pd.notna(ph_price)
                            and pd.notna(price)
                        ):
                            diff = (price - ph_price) / ph_price * 100
                            sign = "▼" if diff < 0 else "▲"
                            color = "#34c759" if diff < 0 else "#ff3b30"
                            st.markdown(
                                f"<div style='font-size:12px;color:{color};'>"
                                f"{sign} {diff:+.1f}% vs клиент</div>",
                                unsafe_allow_html=True,
                            )

        # === MANUAL CORRECTION ROW ===
        _render_manual_actions(row)


def _render_alert_event(ev):
    """Карточка одного alert event."""
    sev_styles = {
        "critical": ("#ffeded", "#ff3b30", "🔴"),
        "warning":  ("#fff5e6", "#ff9500", "⚠️"),
        "info":     ("#eef4fb", "#0066cc", "ℹ️"),
    }
    bg, border, icon = sev_styles.get(ev.severity, sev_styles["info"])
    when = ev.created_at.strftime("%d.%m %H:%M")
    sent = ", ".join(ev.channels_sent or []) or "не отправлено"
    st.markdown(
        f"""
<div style="background:{bg};border-left:3px solid {border};border-radius:8px;
            padding:10px 14px;margin-bottom:6px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <div style="font-size:14px;font-weight:600;color:#1d1d1f;">
      {icon} {ev.title}
    </div>
    <div style="font-size:11px;color:#86868b;">{when}</div>
  </div>
  <div style="font-size:12px;color:#3a3a3c;margin-top:4px;">{ev.detail or ''}</div>
  <div style="font-size:10px;color:#86868b;margin-top:6px;text-transform:uppercase;
              letter-spacing:.04em;">
    {ev.rule_type}  ·  отправлено: {sent}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_manual_actions(row):
    """Кнопки ручной коррекции: ✓ Подтвердить / ✗ Не один товар / 🔍 Заменить."""
    from src import match_actions as ma

    cid = int(row["canonical_id"])
    is_manual = bool(row.get("is_manual", False))

    st.markdown("<div style='margin-top:14px;'></div>", unsafe_allow_html=True)
    ac1, ac2, ac3, ac4 = st.columns([1.2, 1.4, 1.2, 3])

    with ac1:
        if is_manual:
            st.markdown(
                "<div style='padding:7px 12px;background:#e8f7ec;color:#1e6e3a;"
                "border-radius:8px;font-size:13px;text-align:center;font-weight:500;'>"
                "✓ Подтверждено</div>",
                unsafe_allow_html=True,
            )
        else:
            if st.button(
                "✓ Подтвердить", key=f"confirm_{cid}",
                help="Зафиксировать матч — auto-matcher больше не тронет",
                use_container_width=True,
            ):
                with get_session() as s:
                    ma.confirm_match(s, cid)
                reset_caches()
                st.rerun()

    with ac2:
        if st.button(
            "✗ Не один товар", key=f"break_btn_{cid}",
            help="Развязать матч — указать какой Product отвязать",
            use_container_width=True,
        ):
            st.session_state[f"break_open_{cid}"] = not st.session_state.get(
                f"break_open_{cid}", False
            )

    with ac3:
        if st.button(
            "🔍 Заменить...", key=f"swap_btn_{cid}",
            help="Заменить Product на другой кандидат с того же сайта",
            use_container_width=True,
        ):
            st.session_state[f"swap_open_{cid}"] = not st.session_state.get(
                f"swap_open_{cid}", False
            )

    # === Break-flow ===
    if st.session_state.get(f"break_open_{cid}", False):
        with st.container(border=True):
            st.markdown("**Какой товар отвязать от этого матча?**")
            sites_in_match = []
            for site_key, prefix in [
                ("pharmonline", "ph"),
                ("aptekonline", "ap"),
                ("aloe", "al"),
            ]:
                pid = row.get(f"{prefix}_pid")
                if pd.notna(pid):
                    sites_in_match.append((site_key, int(pid)))
            if not sites_in_match:
                st.warning("Нет товаров для отвязки")
            else:
                site_choice = st.radio(
                    "Сайт",
                    options=[s for s, _ in sites_in_match],
                    horizontal=True,
                    key=f"break_radio_{cid}",
                )
                bc1, bc2 = st.columns(2)
                with bc1:
                    if st.button(
                        "Подтвердить отвязку",
                        key=f"break_confirm_{cid}",
                        type="primary",
                        use_container_width=True,
                    ):
                        detach_pid = next(
                            pid for s, pid in sites_in_match if s == site_choice
                        )
                        with get_session() as s:
                            ma.break_match(s, cid, detach_pid, reason="manual break")
                        st.session_state[f"break_open_{cid}"] = False
                        reset_caches()
                        st.success(f"Товар с {site_choice} отвязан")
                        st.rerun()
                with bc2:
                    if st.button(
                        "Отмена",
                        key=f"break_cancel_{cid}",
                        use_container_width=True,
                    ):
                        st.session_state[f"break_open_{cid}"] = False
                        st.rerun()

    # === Swap-flow ===
    if st.session_state.get(f"swap_open_{cid}", False):
        with st.container(border=True):
            st.markdown("**Заменить товар на одном из сайтов**")
            sc1, sc2 = st.columns([1, 3])
            with sc1:
                site_choice = st.selectbox(
                    "Сайт",
                    options=["pharmonline", "aptekonline", "aloe"],
                    key=f"swap_site_{cid}",
                )
            with sc2:
                with get_session() as s:
                    alts = ma.find_alternatives(s, cid, site_choice, limit=30)
                if not alts:
                    st.info(
                        f"На сайте {site_choice} нет несматченных товаров. "
                        "Сначала запусти прогон чтобы появились кандидаты."
                    )
                    new_pid = None
                else:
                    options = {p.id: f"{p.name[:60]}  · score={score}" for p, score in alts}
                    new_pid = st.selectbox(
                        "Заменить на",
                        options=list(options.keys()),
                        format_func=lambda x: options[x],
                        key=f"swap_pid_{cid}",
                    )
            sc3, sc4 = st.columns(2)
            with sc3:
                if alts and st.button(
                    "Заменить",
                    key=f"swap_confirm_{cid}",
                    type="primary",
                    use_container_width=True,
                ):
                    with get_session() as s:
                        ok = ma.swap_alternative(s, cid, site_choice, new_pid)
                    st.session_state[f"swap_open_{cid}"] = False
                    reset_caches()
                    if ok:
                        st.success("Товар заменён")
                    else:
                        st.error("Не удалось заменить")
                    st.rerun()
            with sc4:
                if st.button(
                    "Отмена",
                    key=f"swap_cancel_{cid}",
                    use_container_width=True,
                ):
                    st.session_state[f"swap_open_{cid}"] = False
                    st.rerun()


# ============================================================================
# 🚀 ONBOARDING WIZARD — показывается на главной до полной настройки
# ============================================================================
from src import onboarding as _onb

with get_session() as _ob_session:
    _onb_status = _onb.get_status(_ob_session)

if _onb_status.state != "ready" and not st.session_state.get("onboarding_dismissed", False):
    with st.container(border=True):
        # Прогресс-бар: 4 шага
        steps = [
            ("📦 Данные",  _onb_status.has_runs or _onb_status.has_products),
            ("🗂️ Категории",  _onb_status.has_categories or _onb_status.has_tracked),
            ("📧 Получатели",  _onb_status.has_recipients),
            ("✅ Готово",  False),
        ]
        progress_html = ""
        for label, done in steps[:-1]:
            color = "#34c759" if done else "#d2d2d7"
            progress_html += (
                f"<span style='display:inline-flex;align-items:center;gap:6px;"
                f"padding:6px 14px;border-radius:20px;background:{color}22;"
                f"color:{color if done else '#86868b'};font-size:13px;font-weight:500;"
                f"margin-right:8px;'>{'✓' if done else '○'} {label}</span>"
            )

        hdr_l, hdr_r = st.columns([5, 1])
        with hdr_l:
            st.markdown(
                f"""
<div style='font-size:11px;color:#86868b;letter-spacing:.05em;text-transform:uppercase;
            margin-bottom:6px;'>Setup Wizard</div>
<div style='font-size:22px;font-weight:600;margin-bottom:4px;'>
  Добро пожаловать в Pharmacy Monitor
</div>
<div style='font-size:14px;color:#3a3a3c;margin-bottom:14px;'>
  Несколько шагов чтобы система начала работать.
</div>
<div style='margin-bottom:14px;'>{progress_html}</div>
""",
                unsafe_allow_html=True,
            )
        with hdr_r:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            if st.button(
                "✕ Скрыть",
                key="onb_dismiss",
                help="Скрыть wizard на этой сессии (вернётся при перезагрузке если setup не завершён)",
            ):
                st.session_state["onboarding_dismissed"] = True
                st.rerun()

        if _onb_status.state == "empty_db":
            st.markdown("**Шаг 1 из 3: данные**")
            st.markdown(
                "У тебя пока нет данных в системе. Можно начать с одного из двух путей:"
            )
            wc1, wc2 = st.columns(2)
            with wc1:
                with st.container(border=True):
                    st.markdown("**🌱 Заполнить демо-данными**")
                    st.caption(
                        "30 реалистичных товаров (Paracetamol, Friso, Solgar) на 3 сайтах "
                        "с историей цен — чтобы потрогать дашборд за 5 секунд."
                    )
                    if st.button(
                        "Загрузить demo", type="primary", use_container_width=True,
                        key="onb_seed_demo",
                    ):
                        from src.demo import seed_demo
                        with get_session() as s_:
                            seed_demo(s_, force=True)
                        reset_caches()
                        st.success("✓ Демо-данные загружены")
                        st.rerun()
            with wc2:
                with st.container(border=True):
                    st.markdown("**🚀 Запустить настоящий прогон**")
                    st.caption(
                        "Сначала добавь хотя бы 1 категорию (вкладка 🗂️ Категории) "
                        "или товар в watchlist (📋 Watchlist), потом запусти прогон."
                    )
                    st.markdown(
                        "<a href='#categories' style='color:#0066cc;'>"
                        "→ Перейти к Категориям</a>",
                        unsafe_allow_html=True,
                    )

        elif _onb_status.state == "need_categories":
            st.markdown("**Шаг 2 из 3: категории**")
            st.markdown(
                "Прогоны есть, но нет настроенных категорий или watchlist. "
                "Добавь URL'ы для скрейпинга:"
            )
            wc1, wc2 = st.columns(2)
            with wc1:
                st.markdown(
                    "**🗂️ Категории** — обходим всю категорию по всем 3 сайтам.<br>"
                    "Открой вкладку **Категории** и вставь 3 URL'а.",
                    unsafe_allow_html=True,
                )
            with wc2:
                st.markdown(
                    "**📋 Watchlist** — точечный мониторинг отдельных SKU.<br>"
                    "Открой вкладку **Watchlist** и добавь товар с URL'ами.",
                    unsafe_allow_html=True,
                )

        elif _onb_status.state == "need_recipients":
            st.markdown("**Шаг 3 из 3: получатели**")
            st.markdown(
                "Чтобы получать ежедневный отчёт + алерты — добавь хотя бы один email."
            )
            with st.form("onb_add_recipient"):
                email = st.text_input("Email", placeholder="you@pharmonline.az")
                name = st.text_input("Имя (опц.)", placeholder="Rashad")
                if st.form_submit_button("Добавить", type="primary"):
                    if "@" in email:
                        from src import watchlist as wl_
                        with get_session() as s_:
                            wl_.add_recipient(s_, email, name=name or None)
                        reset_caches()
                        st.success("✓ Готово!")
                        st.rerun()
                    else:
                        st.error("Введи валидный email")
    st.divider()

(
    tab_compare,
    tab_overview,
    tab_analytics,
    tab_alerts,
    tab_inventory,
    tab_categories,
    tab_watchlist,
    tab_recipients,
    tab_settings,
) = st.tabs(
    [
        "🔍 Сравнение цен",
        "📊 Обзор",
        "📈 Аналитика",
        "🔔 Алерты",
        "📦 Склад",
        "🗂️ Категории",
        "📋 Watchlist",
        "📧 Получатели",
        "⚙️ Настройки",
    ]
)

# ============================================================================
# TAB: COMPARISON — главная фича: одинаковые товары на всех 3 сайтах
# ============================================================================
with tab_compare:
    st.subheader("🔍 Сравнение одного и того же товара на 3 сайтах")
    st.caption(
        "Каждая строка — один товар, привязанный ко всем сайтам. "
        "Зелёным выделен самый дешёвый, красным — самый дорогой. "
        "Чтобы товар сюда попал, добавь его в **📋 Watchlist** с URL'ами на каждом сайте."
    )

    cmp_df = load_comparison()

    if cmp_df.empty:
        st.info(
            "**Нет сматченных товаров.**\n\n"
            "Это значит, что система пока не знает какие товары на разных сайтах — "
            "одни и те же.\n\n"
            "👉 **Что делать:**\n"
            "1. Открой вкладку **📋 Watchlist**\n"
            "2. Добавь товар с URL'ами на каждом из 3 сайтов\n"
            "3. Запусти прогон: `uv run pharmacy-monitor run`\n"
            "4. Вернись сюда — здесь появится сравнение"
        )
    else:
        # === Сохранённые виды ===
        with get_session() as _s_views:
            views = wl.list_saved_views(_s_views, scope="comparison")

        if views:
            view_c1, view_c2, view_c3 = st.columns([3, 1, 1])
            with view_c1:
                view_options = ["— без вида —"] + [v.name for v in views]
                chosen_view = st.selectbox(
                    "👁️ Сохранённый вид",
                    options=view_options,
                    key="cmp_saved_view_pick",
                )
                if chosen_view != "— без вида —":
                    selected = next((v for v in views if v.name == chosen_view), None)
                    if selected and selected.params:
                        # Применяем параметры в session_state
                        for k, v in selected.params.items():
                            st.session_state[f"cmp_view_{k}"] = v
            with view_c2:
                if st.button("✏️ Переименовать", key="cmp_view_rename"):
                    if chosen_view != "— без вида —":
                        st.session_state["cmp_view_renaming"] = chosen_view
            with view_c3:
                if st.button("🗑 Удалить", key="cmp_view_del"):
                    if chosen_view != "— без вида —":
                        with get_session() as _s_:
                            wl.delete_saved_view(_s_, chosen_view)
                        st.rerun()

            # Rename form
            if st.session_state.get("cmp_view_renaming"):
                old_name = st.session_state["cmp_view_renaming"]
                with st.form("cmp_view_rename_form", clear_on_submit=True):
                    new_name = st.text_input("Новое имя", value=old_name)
                    rc1, rc2 = st.columns(2)
                    with rc1:
                        if st.form_submit_button("Сохранить", type="primary"):
                            if new_name.strip() and new_name != old_name:
                                with get_session() as _s_:
                                    old = wl.get_saved_view(_s_, old_name)
                                    if old:
                                        params = old.params or {}
                                        scope = old.scope
                                        wl.delete_saved_view(_s_, old_name)
                                        wl.upsert_saved_view(
                                            _s_, new_name.strip(),
                                            params, scope=scope,
                                        )
                            st.session_state.pop("cmp_view_renaming", None)
                            st.rerun()
                    with rc2:
                        if st.form_submit_button("Отмена"):
                            st.session_state.pop("cmp_view_renaming", None)
                            st.rerun()

        # Быстрые фильтры
        col_search, col_min, col_filter, col_sort = st.columns([3, 1.5, 2, 2])
        with col_search:
            search = st.text_input(
                "🔎 Поиск по названию", placeholder="например: Friso, Paracetamol"
            )
        with col_min:
            # Дефолт = 2: показывать матчи на любых 2+ сайтах. Для pilot
            # (где aloe собирает в разы меньше остальных) опция «3» даёт пусто.
            min_sites = st.selectbox(
                "Мин. сайтов",
                options=[3, 2, 1],
                index=1,
                help=(
                    "2 = хотя бы 2 сайта (default — лучше для пилота). "
                    "3 = только товары найденные на ВСЕХ 3 сайтах. "
                    "1 = все товары."
                ),
            )
        with col_filter:
            filter_mode = st.selectbox(
                "Фильтр",
                options=["Все", "Где конкурент дешевле клиента", "На скидке"],
            )
        with col_sort:
            sort_mode = st.selectbox(
                "Сортировка",
                options=[
                    "По названию",
                    "По спреду (макс → мин)",
                    "По цене клиента (мин → макс)",
                ],
            )

        # Кнопка "💾 сохранить как вид" с текущими фильтрами
        with st.expander("💾 Сохранить текущие фильтры как вид", expanded=False):
            with st.form("cmp_save_view", clear_on_submit=True):
                view_name = st.text_input(
                    "Имя вида",
                    placeholder="Например: 'Топ Friso' или 'Только undercut'",
                )
                if st.form_submit_button("Сохранить", type="primary"):
                    if not view_name.strip():
                        st.error("Имя обязательно")
                    else:
                        params = {
                            "search": search,
                            "min_sites": min_sites,
                            "filter_mode": filter_mode,
                            "sort_mode": sort_mode,
                        }
                        with get_session() as _s_:
                            wl.upsert_saved_view(_s_, view_name.strip(), params)
                        st.success(f"✓ Сохранено: '{view_name}'")
                        st.rerun()

        view = cmp_df.copy()
        # Жёсткий фильтр по числу сайтов (главный — по согласованию default=3)
        view = view[view["sites_with_price"] >= min_sites]
        if search:
            view = view[
                view["canonical_name"].str.contains(search, case=False, na=False)
            ]
        if filter_mode == "Где конкурент дешевле клиента":
            mask = (view["ph_price"].notna()) & (
                ((view["ap_price"].notna()) & (view["ap_price"] < view["ph_price"]))
                | ((view["al_price"].notna()) & (view["al_price"] < view["ph_price"]))
            )
            view = view[mask]
        elif filter_mode == "На скидке":
            mask = (view.get("ph_sale", False) > 0) | (view.get("ap_sale", False) > 0) | (view.get("al_sale", False) > 0)
            view = view[mask]

        if sort_mode == "По спреду (макс → мин)":
            view = view.sort_values("spread_pct", ascending=False, na_position="last")
        elif sort_mode == "По цене клиента (мин → макс)":
            view = view.sort_values("ph_price", ascending=True, na_position="last")
        else:
            view = view.sort_values("canonical_name")

        # KPI-полоса по результатам фильтра
        col_a, col_b, col_c, col_d = st.columns(4)
        col_a.metric("Сматченных товаров", len(view))
        col_b.metric(
            "На всех 3 сайтах",
            int((view["sites_with_price"] == 3).sum()) if not view.empty else 0,
        )
        if not view.empty and view["spread_pct"].notna().any():
            col_c.metric("Макс. спред", f"{view['spread_pct'].max():.1f}%")
        else:
            col_c.metric("Макс. спред", "—")
        if not view.empty:
            undercut_n = int(
                (view["cheapest_site"].isin(["aptekonline", "aloe"])).sum()
            )
            col_d.metric("Конкурент дешевле", undercut_n)

        st.divider()

        if view.empty:
            st.info("По текущему фильтру ничего не найдено.")
        else:
            # === Batch confirm: подтвердить все непомеченные матчи на странице ===
            unconfirmed_ids = [
                int(r["canonical_id"]) for _, r in view.iterrows()
                if not bool(r.get("is_manual", False))
            ]
            if unconfirmed_ids:
                bc1, bc2 = st.columns([3, 1])
                with bc1:
                    st.caption(
                        f"📦 На странице **{len(unconfirmed_ids)}** не подтверждённых матчей"
                    )
                with bc2:
                    if st.button(
                        f"✓ Подтвердить все ({len(unconfirmed_ids)})",
                        type="primary",
                        use_container_width=True,
                        help="Помечает все видимые матчи как is_manual=True — auto-matcher "
                             "не будет их трогать.",
                    ):
                        from src import match_actions as ma
                        with get_session() as s_:
                            for cid in unconfirmed_ids:
                                ma.confirm_match(s_, cid)
                        reset_caches()
                        st.success(f"✓ Подтверждено {len(unconfirmed_ids)} матчей")
                        st.rerun()

            # Карточки сравнения — каждая в expander
            for _, row in view.iterrows():
                _render_comparison_card(row)


# ============================================================================
# TAB: OVERVIEW
# ============================================================================
with tab_overview:
    runs = load_runs()
    if runs.empty:
        st.warning(
            "**Прогонов ещё не было.**\n\n"
            "Запусти первый прогон в терминале:\n```\nuv run pharmacy-monitor run --dry-run\n```\n"
            "После этого здесь появятся метрики, графики и сравнение цен."
        )
    else:
        with st.sidebar:
            st.header("⚙️ Фильтры")
            selected_sites = st.multiselect(
                "Сайты", options=list(SITES), default=list(SITES)
            )
            monthly_volume = st.slider(
                "Объём продаж/мес (для ROI)",
                min_value=5,
                max_value=200,
                value=30,
                step=5,
                help=(
                    "Условный объём в единицах для расчёта возможного "
                    "месячного профита/убытка по каждому действию."
                ),
            )
            st.divider()
            st.subheader("Прогоны")
            st.dataframe(
                runs[["id", "started_at", "status", "products_scraped"]].head(10),
                use_container_width=True,
                hide_index=True,
            )

        # ============================================================
        # 🎯 Сегодняшние действия — главная фича Обзора
        # ============================================================
        from src import roi

        with get_session() as s:
            actions = roi.compute_actions(s, assumed_monthly_volume=monthly_volume)
            agg = roi.aggregate_impact(actions)

        col_h1, col_h2 = st.columns([4, 1])
        with col_h1:
            st.subheader("🎯 Сегодняшние действия")
            st.caption(
                "Конкретные шаги на основе свежих цен и матчей. "
                "Прогноз эффекта — при условии что продаётся ~"
                f"{monthly_volume} ед./мес (меняется в сайдбаре)."
            )
        with col_h2:
            # PDF-экспорт последнего отчёта
            if st.button(
                "📄 PDF отчёт",
                use_container_width=True,
                help="Сгенерировать PDF из последнего успешного прогона",
            ):
                from src import analyzer as analyzer_mod, pdf_export, reporter as reporter_mod
                from sqlalchemy import desc as _d, select as _s
                with get_session() as s_pdf:
                    last_run = s_pdf.scalars(
                        _s(storage.Run).where(storage.Run.status == "ok")
                        .order_by(_d(storage.Run.started_at)).limit(1)
                    ).first()
                    if not last_run:
                        st.warning("Нет успешных прогонов")
                    else:
                        with st.spinner("Генерирую PDF (~2 сек)..."):
                            report = analyzer_mod.analyze(s_pdf, last_run.id)
                            html = reporter_mod.render_html(report)
                            try:
                                pdf_bytes = pdf_export.html_to_pdf(html)
                                st.download_button(
                                    "💾 Скачать PDF",
                                    data=pdf_bytes,
                                    file_name=pdf_export.report_pdf_filename(
                                        report.run_started_at
                                    ),
                                    mime="application/pdf",
                                    use_container_width=True,
                                )
                            except Exception as e:
                                st.error(f"Ошибка генерации PDF: {e}")

        # KPI-карточки
        kpi1, kpi2, kpi3, kpi4 = st.columns(4)
        with kpi1:
            st.metric("Возможный профит, ₼/мес", f"+{agg['opportunity']:.0f}")
        with kpi2:
            st.metric("Возможные потери, ₼/мес", f"{agg['loss']:.0f}")
        with kpi3:
            net = agg["total"]
            st.metric(
                "Net эффект, ₼/мес",
                f"{net:+.0f}",
                delta_color="normal" if net >= 0 else "inverse",
            )
        with kpi4:
            st.metric("Всего действий", agg["count"])

        if not actions:
            st.info(
                "Действий нет — данных недостаточно или всё уже оптимизировано. "
                "Запусти прогон или подожди новых матчей."
            )
        else:
            # Фильтр по типу
            type_labels = {
                "price_raise": "💰 Подними цены",
                "undercut": "🔴 Реакция на undercut",
                "assortment_gap": "🆕 Расширь ассортимент",
                "promo_response": "🎯 Промо конкурентов",
            }
            available_types = sorted({a.type for a in actions})
            chosen_types = st.multiselect(
                "Типы действий",
                options=available_types,
                default=available_types,
                format_func=lambda t: type_labels.get(t, t),
                key="action_types_filter",
            )
            filtered = [a for a in actions if a.type in chosen_types]

            for a in filtered[:20]:
                _render_action_card(a)

            if len(filtered) > 20:
                st.caption(f"Показаны первые 20 из {len(filtered)} действий")

        st.divider()

        # ============================================================
        # Метрики и сравнение (как было)
        # ============================================================
        snapshot = load_latest_snapshot()
        if not snapshot.empty:
            snapshot = snapshot[snapshot["site"].isin(selected_sites)]

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Товаров (последний прогон)", len(snapshot))
        with col2:
            on_sale = snapshot[snapshot["is_on_sale"] == 1] if not snapshot.empty else pd.DataFrame()
            st.metric("На скидке", len(on_sale))
        with col3:
            matches_df = load_matches()
            st.metric(
                "Сматченных товаров",
                matches_df["canonical_id"].nunique() if not matches_df.empty else 0,
            )
        with col4:
            promos = load_promos()
            st.metric(
                "Активных промо",
                len(promos[promos["run_id"] == runs.iloc[0]["id"]]) if not promos.empty else 0,
            )

        st.divider()

        st.subheader("🔴 Конкурент опустил цену ниже клиента")
        st.caption(
            "Только сматченные товары. Чтобы увидеть полное сравнение всех цен — "
            "вкладка **🔍 Сравнение цен**."
        )
        if not snapshot.empty and not matches_df.empty:
            snap_eff = snapshot.copy()
            snap_eff["effective_price"] = snap_eff["discount_price"].fillna(snap_eff["price"])
            snap_eff = snap_eff[snap_eff["effective_price"].notna()]
            by_canon = snap_eff[snap_eff["canonical_id"].notna()].copy()
            rows = []
            for canon_id, group in by_canon.groupby("canonical_id"):
                client_rows = group[group["site"] == CLIENT_SITE]
                comp_rows = group[group["site"].isin(COMPETITOR_SITES)]
                if client_rows.empty or comp_rows.empty:
                    continue
                client_price = client_rows["effective_price"].iloc[0]
                client_url = client_rows["url"].iloc[0]
                canon_name = client_rows["name"].iloc[0]
                for _, comp in comp_rows.iterrows():
                    if comp["effective_price"] < client_price:
                        diff_pct = round((client_price - comp["effective_price"]) / client_price * 100, 1)
                        rows.append({
                            "Товар": canon_name,
                            "Клиент ₼": round(client_price, 2),
                            "Конкурент": comp["site"],
                            "Цена конкурента ₼": round(comp["effective_price"], 2),
                            "Дельта %": -diff_pct,
                            "Клиент URL": client_url,
                            "Конкурент URL": comp["url"],
                        })
            if rows:
                undercut_df = pd.DataFrame(rows).sort_values("Дельта %")
                st.dataframe(
                    undercut_df, use_container_width=True, hide_index=True,
                    column_config={
                        "Клиент URL": st.column_config.LinkColumn("Клиент", display_text="open"),
                        "Конкурент URL": st.column_config.LinkColumn("Конкурент", display_text="open"),
                        "Дельта %": st.column_config.NumberColumn(format="%.1f%%"),
                    },
                )
            else:
                st.success("✅ Конкуренты сейчас не дешевле клиента ни по одному сматченному товару.")

        st.divider()

        # PRICE TREND
        st.subheader("📊 Тренд цен по выбранному товару")
        history = load_price_history()
        if not history.empty and not matches_df.empty:
            canon_options = matches_df.groupby("canonical_id")["canonical_name"].first().to_dict()
            canon_id = st.selectbox(
                "Канонический товар",
                options=list(canon_options.keys()),
                format_func=lambda x: canon_options.get(x, str(x)),
            )
            if canon_id:
                canon_history = history[history["canonical_id"] == canon_id]
                if not canon_history.empty:
                    fig = px.line(
                        canon_history, x="run_started_at", y="price", color="site",
                        markers=True,
                        labels={"run_started_at": "Дата прогона", "price": "Цена ₼", "site": "Сайт"},
                        title=canon_options.get(canon_id, "Тренд"),
                    )
                    fig.update_layout(height=420)
                    st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Нет истории — должно быть >=2 прогонов.")

        st.divider()

        st.subheader("🆕 Товары на конкурентах, которых нет у клиента")
        if not snapshot.empty:
            competitor_only = snapshot[
                (snapshot["site"].isin(COMPETITOR_SITES)) & (snapshot["canonical_id"].isna())
            ]
            if not competitor_only.empty:
                gap_df = competitor_only[["site", "name", "category", "discount_price", "price", "url"]].copy()
                gap_df["effective_price"] = gap_df["discount_price"].fillna(gap_df["price"])
                gap_df = gap_df.drop(columns=["discount_price", "price"])
                gap_df.columns = ["Сайт", "Товар", "Категория", "URL", "Цена ₼"]
                st.dataframe(
                    gap_df.head(100), use_container_width=True, hide_index=True,
                    column_config={"URL": st.column_config.LinkColumn(display_text="open")},
                )
                st.caption(f"Показаны первые 100 из {len(gap_df)} несматченных товаров конкурентов")

# ============================================================================
# TAB: ANALYTICS — бренды, промо-история, ассортимент, индекс цен
# ============================================================================
with tab_analytics:
    st.subheader("📈 Аналитика")
    st.caption(
        "Глубинные срезы: распределение брендов, длительность промо-кампаний, "
        "перекрытие ассортимента и ценовой индекс по категориям."
    )

    from src import analytics

    with get_session() as s:
        # 1. Coverage / overlap
        overlap = analytics.assortment_overlap(s)

    o1, o2, o3, o4 = st.columns(4)
    with o1:
        st.metric("Сматченных кластеров", overlap.matched_count)
    with o2:
        st.metric("Coverage клиента, %", f"{overlap.coverage_pct:.0f}%")
    with o3:
        st.metric("Только у клиента", overlap.only_client)
    with o4:
        only_comp_total = sum(overlap.only_competitor_count.values())
        st.metric("Только у конкурентов", only_comp_total)

    if overlap.only_competitor_count:
        st.caption(
            "Эксклюзив конкурентов по сайтам: "
            + " · ".join(
                f"**{site}**: {n}" for site, n in overlap.only_competitor_count.items()
            )
        )

    # === Match quality ===
    with get_session() as s:
        mq = analytics.match_quality(s)
    st.markdown("**🎯 Качество матчинга**")
    mq1, mq2, mq3, mq4 = st.columns(4)
    with mq1:
        st.metric("Auto-матчей", mq.auto_matches)
    with mq2:
        st.metric("Manual ✓", mq.manual_matches)
    with mq3:
        st.metric("Анти-матчей ✗", mq.rejected_pairs)
    with mq4:
        st.metric(
            "Manual %", f"{mq.manual_pct:.0f}%",
            help="Доля матчей подтверждённых вручную. Растёт со временем.",
        )

    st.divider()

    # 2. Price index by category
    st.markdown("**📐 Ценовой индекс по категориям**")
    st.caption(
        "100 = paritet с конкурентами. **<100** — клиент дешевле в среднем. "
        "**>100** — клиент дороже."
    )
    with get_session() as s:
        idx_rows = analytics.price_index_by_category(s)
    if idx_rows:
        idx_df = pd.DataFrame([
            {
                "Категория": x.category,
                "Сматченных SKU": x.matched_skus,
                "Клиент avg ₼": x.avg_client_price,
                "Конкуренты avg ₼": x.avg_competitor_price,
                "Index": x.index,
            }
            for x in idx_rows
        ])
        st.dataframe(
            idx_df, use_container_width=True, hide_index=True,
            column_config={
                "Index": st.column_config.NumberColumn(
                    format="%.1f",
                    help="<100 = клиент дешевле; >100 = клиент дороже"
                ),
            },
        )
    else:
        st.info("Недостаточно сматченных товаров — нужны Match'и на нескольких категориях.")

    st.divider()

    # 3. Brand share
    st.markdown("**🏷️ Бренд-аналитика — топ-30 по числу SKU**")
    with get_session() as s:
        brands = analytics.brand_share(s, top_n=30)
    if brands:
        brand_df = pd.DataFrame([
            {
                "Бренд": b.brand,
                "pharmonline": b.counts.get("pharmonline", 0),
                "aptekonline": b.counts.get("aptekonline", 0),
                "aloe": b.counts.get("aloe", 0),
                "Всего": b.total,
                "Эксклюзив у": b.exclusive_to or "—",
            }
            for b in brands
        ])
        st.dataframe(brand_df, use_container_width=True, hide_index=True)
    else:
        st.info("Нет данных по брендам — проверь что скрейперы заполняют поле brand.")

    st.divider()

    # 4. Forecast / predictive
    st.markdown("**🔮 Прогноз: топ движений и предсказания конкурентов**")
    st.caption(
        "Простая линейная регрессия по последним 30 дням истории цен. "
        "Минимум 3 точки нужно для тренда. **High confidence** — ≥14 точек."
    )

    from src import forecast as forecast_mod

    with get_session() as s:
        movers = forecast_mod.top_movers(s, days_window=30, min_change_pct=3.0, limit=20)
        comp_moves = forecast_mod.predict_competitor_moves(s, days_window=30, max_n=20)

    fc1, fc2 = st.columns(2)
    with fc1:
        st.markdown("**Топ движений по цене (всем сайтам)**")
        if movers:
            mv_df = pd.DataFrame([
                {
                    "Тренд": "📈" if t.direction == "rising"
                            else "📉" if t.direction == "falling" else "➖",
                    "Сайт": t.site,
                    "Товар": t.name[:50],
                    "Было ₼": t.first_price,
                    "Сейчас ₼": t.last_price,
                    "Δ %": t.change_pct,
                    "Прогноз 7д ₼": t.forecast_7d_price,
                    "Уверенность": t.confidence,
                    "Точек": t.n_points,
                }
                for t in movers
            ])
            st.dataframe(mv_df, use_container_width=True, hide_index=True,
                         column_config={"Δ %": st.column_config.NumberColumn(format="%+.1f")})
        else:
            st.info("Нужно ≥3 прогона на товар для расчёта тренда.")

    with fc2:
        st.markdown("**Предсказания: конкуренты, которые могут опустить ещё**")
        if comp_moves:
            cm_df = pd.DataFrame([
                {
                    "Вероятность": {"high": "🔴 Высокая", "medium": "⚠️ Средняя",
                                    "low": "ℹ️ Низкая"}.get(c.probability, c.probability),
                    "Сайт": c.competitor_site,
                    "Товар": c.canonical_name[:40],
                    "Сейчас ₼": c.current_price,
                    "Тренд %": c.trend_7d_change_pct,
                    "Прогноз 7д ₼": c.expected_next_price,
                }
                for c in comp_moves
            ])
            st.dataframe(cm_df, use_container_width=True, hide_index=True,
                         column_config={"Тренд %": st.column_config.NumberColumn(format="%+.1f")})
            st.caption(
                "Конкуренты с падающим трендом — будь готов реагировать "
                "(можно настроить `price_drop_pct` алерт)."
            )
        else:
            st.info("Нет конкурентов с явным трендом снижения.")

    st.divider()

    # 5. Promo history
    st.markdown("**🎯 Промо-история (30 дней)**")
    with get_session() as s:
        promos = analytics.promo_history(s, days=30)
    if promos:
        promo_df = pd.DataFrame([
            {
                "Сайт": p.site,
                "Промо": p.title[:80],
                "Первое появление": p.first_seen.strftime("%d.%m"),
                "Последнее": p.last_seen.strftime("%d.%m"),
                "Дней активна": p.days_active,
                "URL": p.landing_url or "",
            }
            for p in promos
        ])
        st.dataframe(
            promo_df, use_container_width=True, hide_index=True,
            column_config={"URL": st.column_config.LinkColumn(display_text="open")},
        )
    else:
        st.info("Промо-кампаний за последние 30 дней не было.")


# ============================================================================
# TAB: INVENTORY — склад + закупочные цены + маржа
# ============================================================================
with tab_inventory:
    st.subheader("📦 Склад и маржа")
    st.caption(
        "Загрузи CSV с остатками и закупочными ценами — система начнёт учитывать "
        "наличие в alert'ах и пересчитает маржу по каждому SKU."
    )

    from src import inventory as inv_mod

    inv_c1, inv_c2 = st.columns(2)
    with inv_c1:
        with st.container(border=True):
            st.markdown("**📥 Импорт остатков**")
            st.caption("CSV колонки: `sku, qty, name`")
            stock_file = st.file_uploader(
                "stock.csv", type=["csv"], key="stock_upload"
            )
            if stock_file:
                tmp = Path("data") / "_uploaded_stock.csv"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(stock_file.read())
                with get_session() as s_:
                    res = inv_mod.import_stock_from_csv(s_, tmp)
                reset_caches()
                st.success(
                    f"✓ Загружено: total={res.total} matched={res.matched_to_product} "
                    f"unmatched={res.unmatched}"
                )

    with inv_c2:
        with st.container(border=True):
            st.markdown("**💰 Импорт закупочных цен**")
            st.caption(
                "CSV колонки: `sku, supplier_name, purchase_price, currency, name`"
            )
            sup_file = st.file_uploader(
                "purchase_prices.csv", type=["csv"], key="sup_upload"
            )
            if sup_file:
                tmp = Path("data") / "_uploaded_supplier.csv"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(sup_file.read())
                with get_session() as s_:
                    res = inv_mod.import_supplier_prices_from_csv(s_, tmp)
                reset_caches()
                st.success(
                    f"✓ Загружено: total={res.total} matched={res.matched_to_product} "
                    f"unmatched={res.unmatched}"
                )

    st.divider()

    st.markdown("**📊 Маржа-репорт (топ-100)**")
    with get_session() as s:
        margin_rows = inv_mod.margin_report(s)
    if not margin_rows:
        st.info(
            "Нет данных для отчёта о марже. Нужны:\n"
            "1. Прогон с ценами клиента (pharmonline)\n"
            "2. Импорт закупочных цен (форма выше)"
        )
    else:
        margin_df = pd.DataFrame([
            {
                "Stock": "📦" if r.in_stock else "⏸️",
                "Товар": r.name[:60],
                "Sale ₼": r.sale_price,
                "Buy ₼": r.purchase_price,
                "Margin ₼": r.margin_azn,
                "Margin %": r.margin_pct,
            }
            for r in margin_rows[:100]
        ])
        st.dataframe(
            margin_df, use_container_width=True, hide_index=True,
            column_config={
                "Margin %": st.column_config.NumberColumn(format="%.1f%%"),
                "Margin ₼": st.column_config.NumberColumn(format="%+.2f"),
            },
        )
        st.caption(
            f"Показаны топ-100 из {len(margin_rows)}. "
            "Sale = текущая цена клиента (pharmonline), Buy = минимум среди поставщиков."
        )


# ============================================================================
# TAB: ALERTS — правила + лента событий
# ============================================================================
with tab_alerts:
    st.subheader("🔔 Алерты — правила и события")
    st.caption(
        "Правила срабатывают после каждого прогона. События доставляются по выбранным "
        "каналам (email и/или Telegram). Дубликаты подавляются на cooldown_hours."
    )

    from sqlalchemy import desc as _desc, select as _sa_select

    with get_session() as s:
        # === RULES ===
        st.markdown("**📐 Активные правила**")

        rules = s.scalars(
            _sa_select(storage.AlertRule).order_by(storage.AlertRule.id)
        ).all()

        if rules:
            rule_rows = []
            for r in rules:
                rule_rows.append({
                    "ID": r.id,
                    "Активно": r.is_active,
                    "Тип": r.rule_type,
                    "Имя": r.name,
                    "Параметры": str(r.params or {}),
                    "Каналы": ", ".join(r.channels or []),
                    "Cooldown (ч)": r.cooldown_hours,
                })
            edited = st.data_editor(
                pd.DataFrame(rule_rows),
                use_container_width=True, hide_index=True,
                disabled=["ID", "Тип", "Параметры"],
                column_config={"Активно": st.column_config.CheckboxColumn()},
                key="alert_rules_editor",
            )
            for orig, new in zip(rule_rows, edited.to_dict("records")):
                if (
                    orig["Активно"] != new["Активно"]
                    or orig["Имя"] != new["Имя"]
                    or orig["Каналы"] != new["Каналы"]
                    or orig["Cooldown (ч)"] != new["Cooldown (ч)"]
                ):
                    rule = s.get(storage.AlertRule, orig["ID"])
                    if rule:
                        rule.is_active = new["Активно"]
                        rule.name = new["Имя"]
                        rule.channels = [
                            c.strip() for c in new["Каналы"].split(",") if c.strip()
                        ]
                        rule.cooldown_hours = int(new["Cooldown (ч)"])
                    s.commit()
                    reset_caches()
        else:
            st.info("Правил ещё нет. Создай первое ниже.")

        st.divider()

        with st.expander("➕ Добавить правило", expanded=not rules):
            with st.form("add_alert_rule", clear_on_submit=True):
                c1, c2 = st.columns(2)
                with c1:
                    new_type = st.selectbox(
                        "Тип правила",
                        options=[
                            "undercut_threshold",
                            "price_drop_pct",
                            "new_product",
                            "promo_started",
                            "price_raise_opportunity",
                        ],
                        format_func=lambda t: {
                            "undercut_threshold": "🔴 Конкурент дешевле клиента (порог %)",
                            "price_drop_pct": "📉 Резкое падение цены (%)",
                            "new_product": "🆕 Новый товар на сайте",
                            "promo_started": "🎯 Новая промо-кампания",
                            "price_raise_opportunity": "💰 Возможность поднять цену",
                        }.get(t, t),
                    )
                    new_name = st.text_input(
                        "Имя (опц.)", placeholder="Undercut top-50 SKU"
                    )
                with c2:
                    new_min_pct = st.number_input(
                        "Порог % (для threshold-правил)",
                        min_value=0.0, max_value=100.0, value=5.0, step=0.5,
                    )
                    new_cooldown = st.number_input(
                        "Cooldown (часов)", min_value=1, max_value=168, value=12,
                    )
                new_channels = st.multiselect(
                    "Каналы доставки",
                    options=["email", "telegram"],
                    default=["email"],
                )

                if st.form_submit_button("Создать правило", type="primary"):
                    params: dict = {}
                    if new_type in (
                        "undercut_threshold", "price_drop_pct",
                        "price_raise_opportunity",
                    ):
                        params["min_pct"] = float(new_min_pct)
                    rule = storage.AlertRule(
                        name=new_name or new_type,
                        rule_type=new_type,
                        params=params,
                        channels=new_channels or ["email"],
                        cooldown_hours=int(new_cooldown),
                        is_active=True,
                    )
                    s.add(rule)
                    s.commit()
                    reset_caches()
                    st.success(f"Создано: #{rule.id} {rule.rule_type}")
                    st.rerun()

        st.divider()

        # === EVALUATE BUTTON ===
        col_e1, col_e2, col_e3 = st.columns([2, 2, 4])
        with col_e1:
            if st.button(
                "▶️ Прогнать алерты сейчас",
                type="primary",
                use_container_width=True,
                help="Прогон evaluate_rules вручную — без скрейпинга, только перепроверка по последнему прогону",
            ):
                from src import alerts as alerts_mod
                with st.spinner("Прогон..."):
                    fired = alerts_mod.evaluate_rules(s)
                reset_caches()
                st.success(f"Сработало: {len(fired)} событий")
                st.rerun()
        with col_e2:
            if st.button(
                "🗑 Очистить старые события (>30д)",
                use_container_width=True,
            ):
                cutoff = utcnow() - timedelta(days=30)
                deleted = s.execute(
                    storage.AlertEvent.__table__.delete().where(
                        storage.AlertEvent.created_at < cutoff
                    )
                ).rowcount
                s.commit()
                reset_caches()
                st.success(f"Удалено: {deleted}")
                st.rerun()

        st.divider()

        # === RECENT EVENTS FEED ===
        st.markdown("**📜 Последние 50 событий**")
        events = s.scalars(
            _sa_select(storage.AlertEvent)
            .order_by(_desc(storage.AlertEvent.created_at))
            .limit(50)
        ).all()
        if not events:
            st.caption("(пока нет событий — запусти прогон)")
        else:
            for ev in events:
                _render_alert_event(ev)


# ============================================================================
# TAB 2: CATEGORIES
# ============================================================================
with tab_categories:
    st.subheader("🗂️ Категории для скрейпинга")
    st.caption(
        "Просто **вставь URL'ы категорий** (один на строку) — система автоматически "
        "распознает сайт и извлечёт slug. После прогона товары сматчатся между сайтами "
        "и появятся во вкладке **🔍 Сравнение цен**."
    )

    with get_session() as s:
        cats = wl.list_categories(s, active_only=False)
        if not cats:
            with st.spinner("Импорт категорий из config/categories.yaml..."):
                from pathlib import Path as _P
                n = wl.seed_categories_from_yaml(s, _P("config/categories.yaml"))
                if n > 0:
                    st.success(f"Импортировано {n} категорий из YAML")
                    cats = wl.list_categories(s, active_only=False)
                    reset_caches()

        # === ДОБАВЛЕНИЕ ЧЕРЕЗ URL ===
        with st.container(border=True):
            st.markdown("**➕ Добавить категорию через URL**")
            st.caption(
                "Поддерживаются: pharmonline.az/products?category=…, "
                "aptekonline.az/products/{ID}, aloe.az/catalog/filters/?category_slug=…"
            )

            urls_text = st.text_area(
                "URL'ы (по одному на строку — любого порядка)",
                placeholder=(
                    "https://pharmonline.az/products?category=ushaq-qidasi\n"
                    "https://aptekonline.az/products/252\n"
                    "https://aloe.az/catalog/filters/?category_slug=u%C5%9Faq-qidas%C4%B1"
                ),
                key="cat_urls_input",
                height=110,
            )

            # Превью результата парсинга
            preview_found, preview_unknown = ({}, [])
            if urls_text.strip():
                preview_found, preview_unknown = parse_urls_block(urls_text)

            if urls_text.strip():
                pc1, pc2, pc3 = st.columns(3)
                with pc1:
                    if "pharmonline" in preview_found:
                        st.success(f"✅ pharmonline\n`{preview_found['pharmonline']}`")
                    else:
                        st.markdown("⚪ pharmonline — не указан")
                with pc2:
                    if "aptekonline" in preview_found:
                        st.success(f"✅ aptekonline\n`{preview_found['aptekonline']}`")
                    else:
                        st.markdown("⚪ aptekonline — не указан")
                with pc3:
                    if "aloe" in preview_found:
                        st.success(f"✅ aloe\n`{preview_found['aloe']}`")
                    else:
                        st.markdown("⚪ aloe — не указан")

                if preview_unknown:
                    st.warning(f"⚠️ Не распознаны: {len(preview_unknown)} строк(и)")
                    with st.expander("Показать"):
                        for u in preview_unknown:
                            st.code(u)

            with st.form("add_category_url", clear_on_submit=True):
                fc1, fc2, fc3 = st.columns([2, 2, 2])
                with fc1:
                    new_key = st.text_input(
                        "Ключ (латиница, без пробелов)",
                        placeholder="kids_food",
                    )
                with fc2:
                    new_label_ru = st.text_input(
                        "Название (RU)*", placeholder="Детское питание"
                    )
                with fc3:
                    new_label_az = st.text_input(
                        "Название (AZ)", placeholder="Uşaq qidası"
                    )
                submitted = st.form_submit_button(
                    "🔗 Распарсить и добавить", type="primary"
                )

                if submitted:
                    if not urls_text.strip():
                        st.error("Вставь хотя бы один URL выше")
                    elif not new_label_ru.strip():
                        st.error("Название (RU) обязательно")
                    elif not preview_found:
                        st.error(
                            "Ни одного URL не распознано. Проверь формат ссылок."
                        )
                    else:
                        # Авто-генерируем ключ если не задан (из label_ru транслитом)
                        auto_key = (
                            new_key.strip()
                            or new_label_ru.lower().replace(" ", "_")[:40]
                        )
                        cat = wl.add_category(
                            s,
                            key=auto_key,
                            label_ru=new_label_ru.strip(),
                            label_az=new_label_az.strip() or None,
                            pharmonline_slug=preview_found.get("pharmonline"),
                            aptekonline_slug=preview_found.get("aptekonline"),
                            aloe_slug=preview_found.get("aloe"),
                        )
                        reset_caches()
                        st.success(f"✅ Добавлено: #{cat.id} {cat.label_ru}")
                        st.rerun()

        st.divider()

        # === ТАБЛИЦА КАТЕГОРИЙ ===
        st.markdown("**📋 Существующие категории**")

        if not cats:
            st.info("Категорий пока нет. Добавь первую через форму выше.")
        else:
            # Карточки с per-row кнопкой ▶️ Scrape
            for cat in cats:
                with st.container(border=True):
                    cc1, cc2, cc3 = st.columns([4, 5, 2])
                    with cc1:
                        active_emoji = "✅" if cat.is_active else "⏸️"
                        st.markdown(
                            f"{active_emoji} **#{cat.id} · {cat.label_ru}**"
                            + (f"<br><span style='color:#86868b;font-size:12px;'>{cat.label_az}</span>" if cat.label_az else ""),
                            unsafe_allow_html=True,
                        )
                        st.caption(f"Ключ: `{cat.key}`")
                    with cc2:
                        # Сводка по сайтам — какие slug'и заполнены
                        site_chips = []
                        for site_label, slug in [
                            ("pharmonline", cat.pharmonline_slug),
                            ("aptekonline", cat.aptekonline_slug),
                            ("aloe", cat.aloe_slug),
                        ]:
                            if slug:
                                site_chips.append(
                                    f"<span style='background:#e8f4ff;color:#0066cc;"
                                    f"padding:3px 9px;border-radius:6px;font-size:12px;"
                                    f"margin-right:6px;font-family:Menlo,monospace;'>"
                                    f"{site_label}: {slug[:30]}</span>"
                                )
                            else:
                                site_chips.append(
                                    f"<span style='background:#f5f5f7;color:#86868b;"
                                    f"padding:3px 9px;border-radius:6px;font-size:12px;"
                                    f"margin-right:6px;'>{site_label}: —</span>"
                                )
                        st.markdown(
                            "<div style='padding:6px 0;'>" + "".join(site_chips) + "</div>",
                            unsafe_allow_html=True,
                        )
                    with cc3:
                        # ▶️ Запустить scrape ТОЛЬКО для этой категории
                        if st.button(
                            "▶️ Прогон",
                            key=f"run_cat_{cat.id}",
                            use_container_width=True,
                            type="primary",
                            help="Скрейпит только эту категорию по всем сайтам где есть slug",
                        ):
                            with st.spinner(
                                f"Скрейпим '{cat.label_ru}' (~30-90 сек)..."
                            ):
                                try:
                                    rc, log = run_pharmacy_monitor(
                                        dry_run=True, mode="category"
                                    )
                                    # ↑ простой вариант: запускаем все категории.
                                    # Можно использовать --category-id если subprocess возьмёт его.
                                    # Для точечной точки — пробрасываем флаг через extra args:
                                    proc = subprocess.run(
                                        [
                                            "uv", "run", "pharmacy-monitor",
                                            "run", "--mode", "category",
                                            "--category-id", str(cat.id),
                                            "--dry-run",
                                        ],
                                        cwd=PROJECT_ROOT,
                                        capture_output=True,
                                        text=True,
                                        timeout=600,
                                    )
                                    log = (proc.stdout or "") + (proc.stderr or "")
                                    rc = proc.returncode
                                except subprocess.TimeoutExpired:
                                    rc, log = -1, "⛔ Timeout"
                            if rc == 0:
                                reset_caches()
                                st.success(
                                    f"✅ Прогон '{cat.label_ru}' готов. "
                                    "Открой 🔍 Сравнение цен."
                                )
                                with st.expander("📜 Лог"):
                                    st.code(log[-3000:], language="bash")
                                st.rerun()
                            else:
                                st.error(f"❌ Прогон упал (exit {rc})")
                                st.code(log[-3000:], language="bash")

                    # === Inline edit / delete ===
                    with st.expander("✏️ Изменить URL'ы / удалить", expanded=False):
                        # Восстановить URL из slug (показать current state)
                        current_urls_lines = []
                        if cat.pharmonline_slug:
                            current_urls_lines.append(
                                f"https://pharmonline.az/products?category={cat.pharmonline_slug}"
                            )
                        if cat.aptekonline_slug:
                            current_urls_lines.append(
                                f"https://aptekonline.az/products/{cat.aptekonline_slug}"
                            )
                        if cat.aloe_slug:
                            if cat.aloe_slug.startswith("product_field="):
                                current_urls_lines.append(
                                    f"https://aloe.az/catalog/filters/?{cat.aloe_slug}"
                                )
                            else:
                                current_urls_lines.append(
                                    f"https://aloe.az/catalog/filters/?category_slug={cat.aloe_slug}"
                                )

                        edit_urls = st.text_area(
                            "URL'ы",
                            value="\n".join(current_urls_lines),
                            key=f"edit_urls_{cat.id}",
                            height=90,
                        )
                        edit_label = st.text_input(
                            "Название (RU)",
                            value=cat.label_ru,
                            key=f"edit_label_{cat.id}",
                        )
                        edit_active = st.checkbox(
                            "Активна",
                            value=cat.is_active,
                            key=f"edit_active_{cat.id}",
                        )
                        ec1, ec2 = st.columns(2)
                        with ec1:
                            if st.button(
                                "💾 Сохранить",
                                key=f"save_cat_{cat.id}",
                                type="primary",
                                use_container_width=True,
                            ):
                                edit_found, _ = parse_urls_block(edit_urls)
                                wl.update_category(
                                    s,
                                    cat.id,
                                    label_ru=edit_label,
                                    is_active=edit_active,
                                    pharmonline_slug=edit_found.get("pharmonline"),
                                    aptekonline_slug=edit_found.get("aptekonline"),
                                    aloe_slug=edit_found.get("aloe"),
                                )
                                reset_caches()
                                st.success("Сохранено")
                                st.rerun()
                        with ec2:
                            if st.button(
                                "🗑 Удалить",
                                key=f"del_cat_{cat.id}",
                                use_container_width=True,
                            ):
                                wl.remove_category(s, cat.id)
                                reset_caches()
                                st.rerun()


# ============================================================================
# TAB 3: WATCHLIST
# ============================================================================
with tab_watchlist:
    st.subheader("📋 Watchlist — отслеживаемые товары")
    st.caption(
        "Каждый товар можно привязать к конкретной странице на каждом из 3 сайтов. "
        "Когда URL зафиксирован, ежедневный прогон будет ходить именно по нему."
    )

    with st.container(border=True):
        st.markdown("**🚀 Запустить прогон по watchlist**")
        st.caption(
            "Соберёт цены по pinned-URL'ам, привяжет товары через auto-match, "
            "обновит вкладку **🔍 Сравнение цен**."
        )
        render_run_panel(key_prefix="watchlist", default_dry_run=True)
    st.divider()

    with get_session() as s:
        items = wl.list_tracked(s, active_only=False)

        # Display table
        if items:
            rows = []
            for tp in items:
                urls = {link.site: link for link in tp.links}
                rows.append({
                    "ID": tp.id,
                    "Активен": "✓" if tp.is_active else "✗",
                    "Название": tp.canonical_name,
                    "Бренд": tp.brand or "",
                    "Дозировка": tp.dosage or "",
                    "Упаковка": tp.pack_size or "",
                    "pharmonline": urls.get("pharmonline").url if "pharmonline" in urls and urls["pharmonline"].url else "—",
                    "aptekonline": urls.get("aptekonline").url if "aptekonline" in urls and urls["aptekonline"].url else "—",
                    "aloe": urls.get("aloe").url if "aloe" in urls and urls["aloe"].url else "—",
                })
            df = pd.DataFrame(rows)
            st.dataframe(
                df, use_container_width=True, hide_index=True,
                column_config={
                    "pharmonline": st.column_config.LinkColumn(display_text="pharmonline"),
                    "aptekonline": st.column_config.LinkColumn(display_text="aptekonline"),
                    "aloe": st.column_config.LinkColumn(display_text="aloe"),
                },
            )
        else:
            st.info("Watchlist пуст. Добавь первый товар ниже или импортируй CSV.")

        st.divider()

        col_a, col_b = st.columns(2)
        with col_a:
            with st.expander("➕ Добавить товар", expanded=not items):
                with st.form("add_tracked", clear_on_submit=True):
                    name = st.text_input("Название (canonical_name)*")
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        brand = st.text_input("Бренд")
                    with c2:
                        dosage = st.text_input("Дозировка (500mg)")
                    with c3:
                        pack_size = st.text_input("Упаковка (N20)")
                    ph_url = st.text_input("pharmonline URL", placeholder="https://www.pharmonline.az/...")
                    ap_url = st.text_input("aptekonline URL", placeholder="https://www.aptekonline.az/...")
                    al_url = st.text_input("aloe URL", placeholder="https://aloe.az/...")
                    notes = st.text_area("Заметки")
                    submit = st.form_submit_button("Добавить", type="primary")
                    if submit:
                        if not name.strip():
                            st.error("Название обязательно.")
                        else:
                            tp = wl.add_tracked_product(
                                s, name,
                                brand=brand or None,
                                dosage=dosage or None,
                                pack_size=pack_size or None,
                                notes=notes or None,
                                pharmonline_url=ph_url or None,
                                aptekonline_url=ap_url or None,
                                aloe_url=al_url or None,
                            )
                            reset_caches()
                            st.success(f"Добавлено #{tp.id}: {tp.canonical_name}")
                            st.rerun()

        with col_b:
            with st.expander("✏️ Изменить / удалить"):
                if items:
                    tp_options = {tp.id: f"#{tp.id} {tp.canonical_name}" for tp in items}
                    chosen = st.selectbox("Товар", options=list(tp_options.keys()), format_func=lambda x: tp_options[x])
                    chosen_tp = next((t for t in items if t.id == chosen), None)
                    if chosen_tp:
                        with st.form("edit_tracked"):
                            new_name = st.text_input("Название", value=chosen_tp.canonical_name)
                            c1, c2, c3 = st.columns(3)
                            with c1:
                                new_brand = st.text_input("Бренд", value=chosen_tp.brand or "")
                            with c2:
                                new_dosage = st.text_input("Дозировка", value=chosen_tp.dosage or "")
                            with c3:
                                new_pack = st.text_input("Упаковка", value=chosen_tp.pack_size or "")
                            current_urls = {link.site: link.url or "" for link in chosen_tp.links}
                            new_ph = st.text_input("pharmonline URL", value=current_urls.get("pharmonline", ""))
                            new_ap = st.text_input("aptekonline URL", value=current_urls.get("aptekonline", ""))
                            new_al = st.text_input("aloe URL", value=current_urls.get("aloe", ""))
                            is_active = st.checkbox("Активен", value=chosen_tp.is_active)
                            cs, cd = st.columns(2)
                            with cs:
                                save = st.form_submit_button("💾 Сохранить", type="primary")
                            with cd:
                                delete = st.form_submit_button("🗑 Удалить", type="secondary")
                            if save:
                                wl.update_tracked(
                                    s, chosen_tp.id,
                                    canonical_name=new_name,
                                    brand=new_brand or None,
                                    dosage=new_dosage or None,
                                    pack_size=new_pack or None,
                                    is_active=is_active,
                                )
                                wl.set_link_url(s, chosen_tp.id, "pharmonline", new_ph or None,
                                                status="confirmed" if new_ph else "pending")
                                wl.set_link_url(s, chosen_tp.id, "aptekonline", new_ap or None,
                                                status="confirmed" if new_ap else "pending")
                                wl.set_link_url(s, chosen_tp.id, "aloe", new_al or None,
                                                status="confirmed" if new_al else "pending")
                                reset_caches()
                                st.success("Сохранено")
                                st.rerun()
                            if delete:
                                wl.remove_tracked(s, chosen_tp.id)
                                reset_caches()
                                st.success("Удалено")
                                st.rerun()
                else:
                    st.caption("Сначала добавь хотя бы один товар.")

        st.divider()

        with st.expander("📥 Импорт / экспорт CSV"):
            st.markdown("**Колонки CSV:** `canonical_name, brand, dosage, pack_size, search_query, pharmonline_url, aptekonline_url, aloe_url, notes`")
            uploaded = st.file_uploader("Выбрать CSV для импорта", type=["csv"])
            if uploaded:
                tmp_path = Path("data") / "_uploaded_watchlist.csv"
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path.write_bytes(uploaded.read())
                n = wl.import_from_csv(s, tmp_path)
                reset_caches()
                st.success(f"Импортировано {n} товаров")
                st.rerun()

            if items:
                export_path = Path("data") / "watchlist_export.csv"
                if st.button("📤 Экспортировать текущий список в CSV"):
                    n = wl.export_to_csv(s, export_path)
                    st.success(f"Экспортировано {n} в {export_path}")
                    with open(export_path, "rb") as f:
                        st.download_button("Скачать CSV", f, file_name="watchlist.csv", mime="text/csv")

# ============================================================================
# TAB 3: RECIPIENTS
# ============================================================================
with tab_recipients:
    st.subheader("📧 Получатели email-отчёта")
    st.caption(
        "Получатели берутся в порядке: явный аргумент → активные записи здесь → EMAIL_TO в .env. "
        "Чтобы временно отключить получателя, используй переключатель."
    )

    with get_session() as s:
        recipients = wl.list_recipients(s, active_only=False)

        if recipients:
            rows = [{"ID": r.id, "Email": r.email, "Имя": r.name or "", "Активен": r.is_active} for r in recipients]
            edited = st.data_editor(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
                disabled=["ID", "Email"],
                column_config={
                    "Активен": st.column_config.CheckboxColumn(),
                },
                key="recipients_editor",
            )
            # Применяем изменения
            for orig, new in zip(rows, edited.to_dict("records")):
                if orig["Активен"] != new["Активен"]:
                    wl.toggle_recipient(s, orig["Email"])
                    reset_caches()
                if orig["Имя"] != new["Имя"]:
                    wl.update_recipient(s, orig["Email"], name=new["Имя"] or None)
                    reset_caches()
        else:
            st.info("Нет получателей. Добавь первого ниже.")

        st.divider()

        col_add, col_remove = st.columns(2)
        with col_add:
            with st.form("add_recipient", clear_on_submit=True):
                st.write("**➕ Добавить получателя**")
                new_email = st.text_input("Email")
                new_name = st.text_input("Имя (опц.)")
                if st.form_submit_button("Добавить", type="primary"):
                    if not new_email.strip():
                        st.error("Email обязателен")
                    elif "@" not in new_email:
                        st.error("Похоже это не email")
                    else:
                        r = wl.add_recipient(s, new_email, new_name or None)
                        reset_caches()
                        st.success(f"Добавлено: {r.email}")
                        st.rerun()

        with col_remove:
            with st.form("remove_recipient", clear_on_submit=True):
                st.write("**🗑 Удалить получателя**")
                if recipients:
                    email_to_remove = st.selectbox(
                        "Email", options=[r.email for r in recipients]
                    )
                    if st.form_submit_button("Удалить", type="secondary"):
                        ok = wl.remove_recipient(s, email_to_remove)
                        reset_caches()
                        if ok:
                            st.success(f"Удалено: {email_to_remove}")
                            st.rerun()

# ============================================================================
# TAB 5: SETTINGS — обзор конфигурации (read-only)
# ============================================================================
with tab_settings:
    st.subheader("⚙️ Настройки и состояние системы")

    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown("**📦 База данных**")
        st.code(DATABASE_URL)
        st.markdown("**📅 Прогоны**")
        st.metric("Всего прогонов", len(load_runs()))

        st.markdown("**🌐 Скрейпинг — переменные окружения**")
        env_table = pd.DataFrame(
            [
                {"Параметр": "SCRAPE_RATE_LIMIT_SEC", "Значение": os.getenv("SCRAPE_RATE_LIMIT_SEC", "2"), "Описание": "Минимум сек между запросами"},
                {"Параметр": "SCRAPE_TIMEOUT_SEC", "Значение": os.getenv("SCRAPE_TIMEOUT_SEC", "30"), "Описание": "Timeout одного запроса"},
                {"Параметр": "SCRAPE_MAX_RETRIES", "Значение": os.getenv("SCRAPE_MAX_RETRIES", "3"), "Описание": "Кол-во попыток при ошибке"},
                {"Параметр": "SCRAPE_HEADLESS", "Значение": os.getenv("SCRAPE_HEADLESS", "true"), "Описание": "Браузер без UI"},
            ]
        )
        st.dataframe(env_table, use_container_width=True, hide_index=True)
        st.caption("Изменяется в файле `.env` или через export перед запуском")

    with col_right:
        st.markdown("**📧 SMTP (Gmail)**")
        smtp_status = "✓ настроено" if os.getenv("SMTP_PASSWORD") else "✗ НЕ настроено"
        smtp_table = pd.DataFrame(
            [
                {"Параметр": "SMTP_HOST", "Значение": os.getenv("SMTP_HOST", "smtp.gmail.com")},
                {"Параметр": "SMTP_PORT", "Значение": os.getenv("SMTP_PORT", "587")},
                {"Параметр": "SMTP_USER", "Значение": os.getenv("SMTP_USER") or "(не задан)"},
                {"Параметр": "SMTP_PASSWORD", "Значение": "•••••••" if os.getenv("SMTP_PASSWORD") else "(не задан)"},
                {"Параметр": "SMTP_FROM", "Значение": os.getenv("SMTP_FROM") or "(не задан)"},
            ]
        )
        st.dataframe(smtp_table, use_container_width=True, hide_index=True)
        st.caption(f"Статус: **{smtp_status}**")

        st.markdown("**🚀 Команды для VPS / Cron**")
        st.code(
            "# Cron — ежедневно в 06:00 AZT\n"
            "0 6 * * * cd /opt/pharmacy-monitor && uv run pharmacy-monitor run >> logs/cron.log 2>&1\n\n"
            "# Дашборд — на 0.0.0.0 для внешнего доступа\n"
            "uv run streamlit run src/dashboard.py --server.address 0.0.0.0 --server.port 8501",
            language="bash",
        )

    st.divider()
    st.markdown("**🔍 Полезные CLI-команды**")
    st.code(
        "uv run pharmacy-monitor run                    # полный прогон\n"
        "uv run pharmacy-monitor run --dry-run          # без отправки email\n"
        "uv run pharmacy-monitor run --site aloe        # только один сайт\n"
        "uv run pharmacy-monitor scrape                 # без анализа/отчёта\n"
        "uv run pharmacy-monitor report --send          # перевыслать последний отчёт\n"
        "uv run pharmacy-monitor category list          # категории\n"
        "uv run pharmacy-monitor recipient list         # получатели\n"
        "uv run pharmacy-monitor watchlist list         # отслеживаемые товары",
        language="bash",
    )

st.caption(f"Источник: {DATABASE_URL}")
