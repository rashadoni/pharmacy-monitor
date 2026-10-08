-- Схема базы на ревизии 0023_snapshot_confirmed_run — замороженный снимок.
--
-- Этот файл НЕ ПРАВЯТ и не пересобирают. Схема меняется миграцией в
-- migrations/versions/; tests/test_models_match_migrations.py накатывает на
-- этот снимок все миграции после 0023 и сверяет результат с моделями.
-- Если тест красный, чинится миграция или модель, а не снимок.
--
-- Откуда взят (2026-10-08): пустой PostgreSQL 16 -> `alembic upgrade head` на
-- коммите, где модели и миграции совпали -> `pg_dump --schema-only --no-owner
-- --no-privileges`; убраны комментарии, строки SET и мета-команды psql,
-- добавлена отметка ревизии. На пустой базе 0001_initial строит схему из
-- моделей, так что это модели того коммита, а не слепок боевой базы: чем
-- боевая отличается — docs/RUNBOOK.md «Модели и миграции».
--
-- Повторить этот рецепт на новом коммите НЕЛЬЗЯ: пустая база примет любую
-- модель без миграции, и снимок её узаконит. Если снимок когда-нибудь придётся
-- сдвинуть на новую ревизию — только от него самого: этот файл ->
-- `alembic upgrade <ревизия>` -> `pg_dump --schema-only`.

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);

CREATE TABLE public.alert_events (
    id integer NOT NULL,
    rule_id integer,
    rule_type character varying(50) NOT NULL,
    dedup_key character varying(300) NOT NULL,
    severity character varying(20) NOT NULL,
    title character varying(500) NOT NULL,
    detail text,
    payload json,
    channels_sent json,
    created_at timestamp without time zone NOT NULL,
    tenant_id integer NOT NULL,
    is_read boolean DEFAULT false NOT NULL,
    read_at timestamp without time zone,
    snoozed_until timestamp without time zone
);

CREATE SEQUENCE public.alert_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.alert_events_id_seq OWNED BY public.alert_events.id;

CREATE TABLE public.alert_rules (
    id integer NOT NULL,
    name character varying(200) NOT NULL,
    rule_type character varying(50) NOT NULL,
    params json,
    channels json,
    cooldown_hours integer NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.alert_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.alert_rules_id_seq OWNED BY public.alert_rules.id;

CREATE TABLE public.aloe_country_mappings (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    country_id character varying(40) NOT NULL,
    country_code character varying(2) NOT NULL,
    country_raw character varying(160) NOT NULL,
    source_url text NOT NULL,
    sample_count integer NOT NULL,
    version integer NOT NULL,
    verified_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.aloe_country_mappings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.aloe_country_mappings_id_seq OWNED BY public.aloe_country_mappings.id;

CREATE TABLE public.audit_logs (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    actor_user_id integer,
    action character varying(20) NOT NULL,
    resource character varying(500) NOT NULL,
    response_status integer NOT NULL,
    request_id character varying(128),
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.audit_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.audit_logs_id_seq OWNED BY public.audit_logs.id;

CREATE TABLE public.categories (
    id integer NOT NULL,
    key character varying(100) NOT NULL,
    label_ru character varying(200) NOT NULL,
    label_az character varying(200),
    pharmonline_slug character varying(200),
    aptekonline_slug character varying(200),
    aloe_slug character varying(200),
    is_active boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.categories_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.categories_id_seq OWNED BY public.categories.id;

CREATE TABLE public.cost_import_batches (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    actor_user_id integer,
    filename character varying(255),
    rows_processed integer NOT NULL,
    rows_imported integer NOT NULL,
    rows_skipped integer NOT NULL,
    changes json,
    created_at timestamp without time zone NOT NULL,
    rolled_back_at timestamp without time zone,
    rolled_back_by_user_id integer
);

CREATE SEQUENCE public.cost_import_batches_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.cost_import_batches_id_seq OWNED BY public.cost_import_batches.id;

CREATE TABLE public.match_policy_audits (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    match_id integer,
    action character varying(40) NOT NULL,
    payload json NOT NULL,
    created_at timestamp without time zone NOT NULL,
    rolled_back_at timestamp without time zone
);

CREATE SEQUENCE public.match_policy_audits_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.match_policy_audits_id_seq OWNED BY public.match_policy_audits.id;

CREATE TABLE public.match_rejections (
    id integer NOT NULL,
    product_a_id integer NOT NULL,
    product_b_id integer NOT NULL,
    reason character varying(200),
    reason_type character varying(40) NOT NULL,
    metadata_json json,
    is_active boolean NOT NULL,
    resolved_at timestamp without time zone,
    updated_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    tenant_id integer NOT NULL
);

CREATE SEQUENCE public.match_rejections_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.match_rejections_id_seq OWNED BY public.match_rejections.id;

CREATE TABLE public.matches (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    canonical_name character varying(500) NOT NULL,
    canonical_brand character varying(200),
    canonical_dosage character varying(100),
    canonical_pack_size character varying(100),
    confidence double precision NOT NULL,
    is_manual boolean NOT NULL,
    match_strategy character varying(30),
    needs_review boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.matches_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.matches_id_seq OWNED BY public.matches.id;

CREATE TABLE public.offer_observations (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    run_id integer NOT NULL,
    product_id integer NOT NULL,
    country_code character varying(2),
    country_raw character varying(160),
    country_resolution_status character varying(20) NOT NULL,
    country_source character varying(40),
    availability_status character varying(20) NOT NULL,
    quantity double precision,
    availability_source character varying(40),
    observed_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.offer_observations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.offer_observations_id_seq OWNED BY public.offer_observations.id;

CREATE TABLE public.pharmonline_public_api_catalog_baselines (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    catalog_item_count integer NOT NULL,
    minimum_catalog_item_count integer NOT NULL,
    verified_identity_count integer NOT NULL,
    trusted_ddp_item_count integer NOT NULL,
    retired_ddp_item_count integer NOT NULL,
    reconciled_item_count integer NOT NULL,
    proof_version character varying(40) NOT NULL,
    source_manifest_sha256 character varying(64) NOT NULL,
    catalog_fingerprint_sha256 character varying(64) NOT NULL,
    source_transport character varying(40) NOT NULL,
    preflight_run_ref character varying(128) NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.pharmonline_public_api_catalog_baselines_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.pharmonline_public_api_catalog_baselines_id_seq OWNED BY public.pharmonline_public_api_catalog_baselines.id;

CREATE TABLE public.pharmonline_public_api_identity_admissions (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    product_id integer NOT NULL,
    admission_kind character varying(40) NOT NULL,
    public_api_external_id character varying(200) NOT NULL,
    public_api_canonical_url character varying(500) NOT NULL,
    proof_version character varying(40) NOT NULL,
    source_manifest_sha256 character varying(64) NOT NULL,
    catalog_fingerprint_sha256 character varying(64) NOT NULL,
    source_transport character varying(40) NOT NULL,
    preflight_run_ref character varying(128) NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.pharmonline_public_api_identity_admissions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.pharmonline_public_api_identity_admissions_id_seq OWNED BY public.pharmonline_public_api_identity_admissions.id;

CREATE TABLE public.pharmonline_public_api_identity_quarantines (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    legacy_product_id integer NOT NULL,
    replacement_product_id integer NOT NULL,
    quarantine_kind character varying(40) NOT NULL,
    legacy_external_id character varying(200) NOT NULL,
    archived_external_id character varying(200) NOT NULL,
    legacy_canonical_url character varying(500) NOT NULL,
    public_api_external_id character varying(200) NOT NULL,
    public_api_canonical_url character varying(500) NOT NULL,
    proof_version character varying(40) NOT NULL,
    source_manifest_sha256 character varying(64) NOT NULL,
    catalog_fingerprint_sha256 character varying(64) NOT NULL,
    source_transport character varying(40) NOT NULL,
    preflight_run_ref character varying(128) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    CONSTRAINT ck_pharmonline_public_api_quarantine_distinct_products CHECK ((legacy_product_id <> replacement_product_id))
);

CREATE SEQUENCE public.pharmonline_public_api_identity_quarantines_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.pharmonline_public_api_identity_quarantines_id_seq OWNED BY public.pharmonline_public_api_identity_quarantines.id;

CREATE TABLE public.pharmonline_public_api_identity_reconciliations (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    product_id integer NOT NULL,
    legacy_external_id character varying(200) NOT NULL,
    public_api_external_id character varying(200) NOT NULL,
    legacy_canonical_url character varying(500) NOT NULL,
    public_api_canonical_url character varying(500) NOT NULL,
    proof_version character varying(40) NOT NULL,
    source_manifest_sha256 character varying(64) NOT NULL,
    catalog_fingerprint_sha256 character varying(64) NOT NULL,
    source_transport character varying(40) NOT NULL,
    preflight_run_ref character varying(128) NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.pharmonline_public_api_identity_reconciliations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.pharmonline_public_api_identity_reconciliations_id_seq OWNED BY public.pharmonline_public_api_identity_reconciliations.id;

CREATE TABLE public.price_snapshots (
    id integer NOT NULL,
    run_id integer NOT NULL,
    product_id integer NOT NULL,
    price double precision,
    discount_price double precision,
    discount_percent double precision,
    is_on_sale boolean NOT NULL,
    promo_label character varying(200),
    captured_at timestamp without time zone NOT NULL,
    confirmed_run_id integer
);

CREATE SEQUENCE public.price_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.price_snapshots_id_seq OWNED BY public.price_snapshots.id;

CREATE TABLE public.pricing_config (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    raise_threshold_pct double precision NOT NULL,
    undercut_threshold_pct double precision NOT NULL,
    max_spread_pct double precision NOT NULL,
    min_margin_pct double precision NOT NULL,
    max_per_type integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.pricing_config_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.pricing_config_id_seq OWNED BY public.pricing_config.id;

CREATE TABLE public.products (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    site character varying(50) NOT NULL,
    external_id character varying(200) NOT NULL,
    url character varying(500) NOT NULL,
    name character varying(500) NOT NULL,
    brand character varying(200),
    manufacturer character varying(200),
    manufacturer_country_code character varying(2),
    manufacturer_country_raw character varying(160),
    country_resolution_status character varying(20) NOT NULL,
    country_source character varying(40),
    country_observed_at timestamp without time zone,
    country_candidate_code character varying(2),
    country_candidate_seen_count integer NOT NULL,
    country_candidate_observed_at timestamp without time zone,
    country_candidate_run_id integer,
    offer_availability_status character varying(20) NOT NULL,
    offer_quantity double precision,
    availability_source character varying(40),
    availability_observed_at timestamp without time zone,
    availability_run_id integer,
    category character varying(100),
    manual_category_key character varying(100),
    dosage character varying(100),
    pack_size character varying(100),
    image_url character varying(500),
    description text,
    barcode character varying(40),
    name_normalized character varying(500) NOT NULL,
    first_seen_at timestamp without time zone NOT NULL,
    last_seen_at timestamp without time zone NOT NULL,
    canonical_id integer,
    url_dead_at timestamp without time zone,
    brand_verified character varying(200)
);

CREATE SEQUENCE public.products_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.products_id_seq OWNED BY public.products.id;

CREATE TABLE public.promos (
    id integer NOT NULL,
    run_id integer NOT NULL,
    site character varying(50) NOT NULL,
    title character varying(500) NOT NULL,
    description text,
    image_url character varying(500),
    landing_url character varying(500),
    valid_until character varying(50),
    raw_data json,
    captured_at timestamp without time zone NOT NULL,
    tenant_id integer NOT NULL
);

CREATE SEQUENCE public.promos_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.promos_id_seq OWNED BY public.promos.id;

CREATE TABLE public.recipients (
    id integer NOT NULL,
    email character varying(200) NOT NULL,
    name character varying(200),
    telegram_chat_id character varying(50),
    is_active boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.recipients_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.recipients_id_seq OWNED BY public.recipients.id;

CREATE TABLE public.roi_actions_cache (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    client_site character varying(32) NOT NULL,
    payload json NOT NULL,
    computed_at timestamp without time zone NOT NULL,
    run_id integer,
    policy_fingerprint character varying(80) NOT NULL,
    trust_epoch character varying(240) NOT NULL
);

CREATE SEQUENCE public.roi_actions_cache_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.roi_actions_cache_id_seq OWNED BY public.roi_actions_cache.id;

CREATE TABLE public.runs (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    started_at timestamp without time zone NOT NULL,
    finished_at timestamp without time zone,
    status character varying(20) NOT NULL,
    error_message text,
    products_scraped integer NOT NULL,
    sites_completed character varying(200),
    products_per_site json,
    products_per_site_category json,
    run_quality json,
    catalog_scope character varying(20) NOT NULL,
    full_catalog_sites character varying(200),
    catalog_verified boolean NOT NULL,
    catalog_verification_reason character varying(300)
);

CREATE SEQUENCE public.runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.runs_id_seq OWNED BY public.runs.id;

CREATE TABLE public.saved_views (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    scope character varying(50) NOT NULL,
    params json,
    is_default boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    tenant_id integer NOT NULL
);

CREATE SEQUENCE public.saved_views_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.saved_views_id_seq OWNED BY public.saved_views.id;

CREATE TABLE public.scrape_requests (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    requested_by_user_id integer,
    mode character varying(20) NOT NULL,
    category_id integer,
    sites character varying(200),
    status character varying(20) NOT NULL,
    requested_at timestamp without time zone NOT NULL,
    started_at timestamp without time zone,
    completed_at timestamp without time zone,
    run_id integer,
    error_message text
);

CREATE SEQUENCE public.scrape_requests_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.scrape_requests_id_seq OWNED BY public.scrape_requests.id;

CREATE TABLE public.stock_levels (
    id integer NOT NULL,
    product_id integer,
    canonical_id integer,
    sku character varying(200),
    name character varying(500),
    qty double precision NOT NULL,
    is_in_stock boolean NOT NULL,
    source character varying(50) NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.stock_levels_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.stock_levels_id_seq OWNED BY public.stock_levels.id;

CREATE TABLE public.supplier_prices (
    id integer NOT NULL,
    product_id integer,
    canonical_id integer,
    sku character varying(200),
    name character varying(500),
    supplier_name character varying(200) NOT NULL,
    purchase_price double precision NOT NULL,
    currency character varying(10) NOT NULL,
    source character varying(50) NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.supplier_prices_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.supplier_prices_id_seq OWNED BY public.supplier_prices.id;

CREATE TABLE public.tenant_users (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    email character varying(200) NOT NULL,
    name character varying(200),
    role character varying(20) NOT NULL,
    password_hash character varying(200),
    magic_token character varying(100),
    magic_token_expires_at timestamp without time zone,
    last_login_at timestamp without time zone,
    is_active boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    telegram_chat_id character varying(50),
    email_severity_min character varying(20),
    telegram_severity_min character varying(20),
    quiet_hours character varying(20),
    daily_digest boolean NOT NULL,
    weekly_digest boolean NOT NULL
);

CREATE SEQUENCE public.tenant_users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.tenant_users_id_seq OWNED BY public.tenant_users.id;

CREATE TABLE public.tenants (
    id integer NOT NULL,
    slug character varying(100) NOT NULL,
    name character varying(200) NOT NULL,
    client_site character varying(100),
    is_active boolean NOT NULL,
    plan character varying(50) NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.tenants_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.tenants_id_seq OWNED BY public.tenants.id;

CREATE TABLE public.tracked_categories (
    id integer NOT NULL,
    tenant_id integer NOT NULL,
    category_id integer NOT NULL,
    notes text,
    is_active boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE public.tracked_categories_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.tracked_categories_id_seq OWNED BY public.tracked_categories.id;

CREATE TABLE public.tracked_product_links (
    id integer NOT NULL,
    tracked_product_id integer NOT NULL,
    site character varying(50) NOT NULL,
    url character varying(500),
    external_id character varying(200),
    status character varying(20) NOT NULL,
    last_checked_at timestamp without time zone
);

CREATE SEQUENCE public.tracked_product_links_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.tracked_product_links_id_seq OWNED BY public.tracked_product_links.id;

CREATE TABLE public.tracked_products (
    id integer NOT NULL,
    canonical_name character varying(500) NOT NULL,
    brand character varying(200),
    dosage character varying(100),
    pack_size character varying(100),
    search_query character varying(500),
    is_active boolean NOT NULL,
    notes text,
    created_at timestamp without time zone NOT NULL,
    tenant_id integer NOT NULL
);

CREATE SEQUENCE public.tracked_products_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.tracked_products_id_seq OWNED BY public.tracked_products.id;

ALTER TABLE ONLY public.alert_events ALTER COLUMN id SET DEFAULT nextval('public.alert_events_id_seq'::regclass);

ALTER TABLE ONLY public.alert_rules ALTER COLUMN id SET DEFAULT nextval('public.alert_rules_id_seq'::regclass);

ALTER TABLE ONLY public.aloe_country_mappings ALTER COLUMN id SET DEFAULT nextval('public.aloe_country_mappings_id_seq'::regclass);

ALTER TABLE ONLY public.audit_logs ALTER COLUMN id SET DEFAULT nextval('public.audit_logs_id_seq'::regclass);

ALTER TABLE ONLY public.categories ALTER COLUMN id SET DEFAULT nextval('public.categories_id_seq'::regclass);

ALTER TABLE ONLY public.cost_import_batches ALTER COLUMN id SET DEFAULT nextval('public.cost_import_batches_id_seq'::regclass);

ALTER TABLE ONLY public.match_policy_audits ALTER COLUMN id SET DEFAULT nextval('public.match_policy_audits_id_seq'::regclass);

ALTER TABLE ONLY public.match_rejections ALTER COLUMN id SET DEFAULT nextval('public.match_rejections_id_seq'::regclass);

ALTER TABLE ONLY public.matches ALTER COLUMN id SET DEFAULT nextval('public.matches_id_seq'::regclass);

ALTER TABLE ONLY public.offer_observations ALTER COLUMN id SET DEFAULT nextval('public.offer_observations_id_seq'::regclass);

ALTER TABLE ONLY public.pharmonline_public_api_catalog_baselines ALTER COLUMN id SET DEFAULT nextval('public.pharmonline_public_api_catalog_baselines_id_seq'::regclass);

ALTER TABLE ONLY public.pharmonline_public_api_identity_admissions ALTER COLUMN id SET DEFAULT nextval('public.pharmonline_public_api_identity_admissions_id_seq'::regclass);

ALTER TABLE ONLY public.pharmonline_public_api_identity_quarantines ALTER COLUMN id SET DEFAULT nextval('public.pharmonline_public_api_identity_quarantines_id_seq'::regclass);

ALTER TABLE ONLY public.pharmonline_public_api_identity_reconciliations ALTER COLUMN id SET DEFAULT nextval('public.pharmonline_public_api_identity_reconciliations_id_seq'::regclass);

ALTER TABLE ONLY public.price_snapshots ALTER COLUMN id SET DEFAULT nextval('public.price_snapshots_id_seq'::regclass);

ALTER TABLE ONLY public.pricing_config ALTER COLUMN id SET DEFAULT nextval('public.pricing_config_id_seq'::regclass);

ALTER TABLE ONLY public.products ALTER COLUMN id SET DEFAULT nextval('public.products_id_seq'::regclass);

ALTER TABLE ONLY public.promos ALTER COLUMN id SET DEFAULT nextval('public.promos_id_seq'::regclass);

ALTER TABLE ONLY public.recipients ALTER COLUMN id SET DEFAULT nextval('public.recipients_id_seq'::regclass);

ALTER TABLE ONLY public.roi_actions_cache ALTER COLUMN id SET DEFAULT nextval('public.roi_actions_cache_id_seq'::regclass);

ALTER TABLE ONLY public.runs ALTER COLUMN id SET DEFAULT nextval('public.runs_id_seq'::regclass);

ALTER TABLE ONLY public.saved_views ALTER COLUMN id SET DEFAULT nextval('public.saved_views_id_seq'::regclass);

ALTER TABLE ONLY public.scrape_requests ALTER COLUMN id SET DEFAULT nextval('public.scrape_requests_id_seq'::regclass);

ALTER TABLE ONLY public.stock_levels ALTER COLUMN id SET DEFAULT nextval('public.stock_levels_id_seq'::regclass);

ALTER TABLE ONLY public.supplier_prices ALTER COLUMN id SET DEFAULT nextval('public.supplier_prices_id_seq'::regclass);

ALTER TABLE ONLY public.tenant_users ALTER COLUMN id SET DEFAULT nextval('public.tenant_users_id_seq'::regclass);

ALTER TABLE ONLY public.tenants ALTER COLUMN id SET DEFAULT nextval('public.tenants_id_seq'::regclass);

ALTER TABLE ONLY public.tracked_categories ALTER COLUMN id SET DEFAULT nextval('public.tracked_categories_id_seq'::regclass);

ALTER TABLE ONLY public.tracked_product_links ALTER COLUMN id SET DEFAULT nextval('public.tracked_product_links_id_seq'::regclass);

ALTER TABLE ONLY public.tracked_products ALTER COLUMN id SET DEFAULT nextval('public.tracked_products_id_seq'::regclass);

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);

ALTER TABLE ONLY public.alert_events
    ADD CONSTRAINT alert_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.alert_rules
    ADD CONSTRAINT alert_rules_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.aloe_country_mappings
    ADD CONSTRAINT aloe_country_mappings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cost_import_batches
    ADD CONSTRAINT cost_import_batches_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.match_policy_audits
    ADD CONSTRAINT match_policy_audits_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.match_rejections
    ADD CONSTRAINT match_rejections_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.matches
    ADD CONSTRAINT matches_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.offer_observations
    ADD CONSTRAINT offer_observations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.pharmonline_public_api_catalog_baselines
    ADD CONSTRAINT pharmonline_public_api_catalog_baselines_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_admissions
    ADD CONSTRAINT pharmonline_public_api_identity_admissions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_quarantines
    ADD CONSTRAINT pharmonline_public_api_identity_quarantines_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_reconciliations
    ADD CONSTRAINT pharmonline_public_api_identity_reconciliations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.price_snapshots
    ADD CONSTRAINT price_snapshots_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.pricing_config
    ADD CONSTRAINT pricing_config_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.promos
    ADD CONSTRAINT promos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.recipients
    ADD CONSTRAINT recipients_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.roi_actions_cache
    ADD CONSTRAINT roi_actions_cache_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT saved_views_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.scrape_requests
    ADD CONSTRAINT scrape_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.stock_levels
    ADD CONSTRAINT stock_levels_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.supplier_prices
    ADD CONSTRAINT supplier_prices_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tenant_users
    ADD CONSTRAINT tenant_users_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tracked_categories
    ADD CONSTRAINT tracked_categories_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tracked_product_links
    ADD CONSTRAINT tracked_product_links_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tracked_products
    ADD CONSTRAINT tracked_products_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.aloe_country_mappings
    ADD CONSTRAINT uq_aloe_country_mapping UNIQUE (tenant_id, country_id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_admissions
    ADD CONSTRAINT uq_pharmonline_public_api_admission_product UNIQUE (tenant_id, product_id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_admissions
    ADD CONSTRAINT uq_pharmonline_public_api_admission_public_id UNIQUE (tenant_id, public_api_external_id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_quarantines
    ADD CONSTRAINT uq_pharmonline_public_api_quarantine_legacy_product UNIQUE (tenant_id, legacy_product_id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_quarantines
    ADD CONSTRAINT uq_pharmonline_public_api_quarantine_public_id UNIQUE (tenant_id, public_api_external_id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_quarantines
    ADD CONSTRAINT uq_pharmonline_public_api_quarantine_replacement_product UNIQUE (tenant_id, replacement_product_id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_reconciliations
    ADD CONSTRAINT uq_pharmonline_public_api_reconciliation_legacy_id UNIQUE (tenant_id, legacy_external_id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_reconciliations
    ADD CONSTRAINT uq_pharmonline_public_api_reconciliation_product UNIQUE (tenant_id, product_id);

ALTER TABLE ONLY public.pharmonline_public_api_identity_reconciliations
    ADD CONSTRAINT uq_pharmonline_public_api_reconciliation_public_id UNIQUE (tenant_id, public_api_external_id);

ALTER TABLE ONLY public.match_rejections
    ADD CONSTRAINT uq_rejection_pair UNIQUE (product_a_id, product_b_id);

ALTER TABLE ONLY public.roi_actions_cache
    ADD CONSTRAINT uq_roi_cache_tenant_site UNIQUE (tenant_id, client_site);

ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT uq_saved_view_name UNIQUE (name);

ALTER TABLE ONLY public.products
    ADD CONSTRAINT uq_site_extid UNIQUE (site, external_id);

ALTER TABLE ONLY public.supplier_prices
    ADD CONSTRAINT uq_supplier_price_product_supplier UNIQUE (product_id, supplier_name);

ALTER TABLE ONLY public.tenant_users
    ADD CONSTRAINT uq_tenant_email UNIQUE (tenant_id, email);

ALTER TABLE ONLY public.tracked_categories
    ADD CONSTRAINT uq_tracked_category_tenant_category UNIQUE (tenant_id, category_id);

ALTER TABLE ONLY public.tracked_product_links
    ADD CONSTRAINT uq_tracked_site UNIQUE (tracked_product_id, site);

CREATE INDEX ix_alert_events_created_at ON public.alert_events USING btree (created_at);

CREATE INDEX ix_alert_events_dedup_key ON public.alert_events USING btree (dedup_key);

CREATE INDEX ix_alert_events_rule_id ON public.alert_events USING btree (rule_id);

CREATE INDEX ix_alert_events_rule_type ON public.alert_events USING btree (rule_type);

CREATE INDEX ix_alert_events_snoozed_until ON public.alert_events USING btree (snoozed_until);

CREATE INDEX ix_alert_events_tenant_id ON public.alert_events USING btree (tenant_id);

CREATE INDEX ix_alert_rules_rule_type ON public.alert_rules USING btree (rule_type);

CREATE INDEX ix_aloe_country_mappings_country_code ON public.aloe_country_mappings USING btree (country_code);

CREATE INDEX ix_aloe_country_mappings_country_id ON public.aloe_country_mappings USING btree (country_id);

CREATE INDEX ix_aloe_country_mappings_tenant_id ON public.aloe_country_mappings USING btree (tenant_id);

CREATE INDEX ix_audit_logs_actor_user_id ON public.audit_logs USING btree (actor_user_id);

CREATE INDEX ix_audit_logs_created_at ON public.audit_logs USING btree (created_at);

CREATE INDEX ix_audit_logs_request_id ON public.audit_logs USING btree (request_id);

CREATE INDEX ix_audit_logs_resource ON public.audit_logs USING btree (resource);

CREATE INDEX ix_audit_logs_tenant_id ON public.audit_logs USING btree (tenant_id);

CREATE UNIQUE INDEX ix_categories_key ON public.categories USING btree (key);

CREATE INDEX ix_cost_import_batches_created_at ON public.cost_import_batches USING btree (created_at);

CREATE INDEX ix_cost_import_batches_tenant_id ON public.cost_import_batches USING btree (tenant_id);

CREATE INDEX ix_match_policy_audits_action ON public.match_policy_audits USING btree (action);

CREATE INDEX ix_match_policy_audits_created_at ON public.match_policy_audits USING btree (created_at);

CREATE INDEX ix_match_policy_audits_match_id ON public.match_policy_audits USING btree (match_id);

CREATE INDEX ix_match_policy_audits_tenant_id ON public.match_policy_audits USING btree (tenant_id);

CREATE INDEX ix_match_rejections_is_active ON public.match_rejections USING btree (is_active);

CREATE INDEX ix_match_rejections_product_a_id ON public.match_rejections USING btree (product_a_id);

CREATE INDEX ix_match_rejections_product_b_id ON public.match_rejections USING btree (product_b_id);

CREATE INDEX ix_match_rejections_reason_type ON public.match_rejections USING btree (reason_type);

CREATE INDEX ix_match_rejections_tenant_id ON public.match_rejections USING btree (tenant_id);

CREATE INDEX ix_matches_tenant_id ON public.matches USING btree (tenant_id);

CREATE INDEX ix_offer_observations_availability_status ON public.offer_observations USING btree (availability_status);

CREATE INDEX ix_offer_observations_country_code ON public.offer_observations USING btree (country_code);

CREATE INDEX ix_offer_observations_observed_at ON public.offer_observations USING btree (observed_at);

CREATE INDEX ix_offer_observations_product_id ON public.offer_observations USING btree (product_id);

CREATE INDEX ix_offer_observations_run_id ON public.offer_observations USING btree (run_id);

CREATE INDEX ix_offer_observations_tenant_id ON public.offer_observations USING btree (tenant_id);

CREATE INDEX ix_pharmonline_public_api_admissions_created_at ON public.pharmonline_public_api_identity_admissions USING btree (created_at);

CREATE INDEX ix_pharmonline_public_api_admissions_product_id ON public.pharmonline_public_api_identity_admissions USING btree (product_id);

CREATE INDEX ix_pharmonline_public_api_admissions_public_id ON public.pharmonline_public_api_identity_admissions USING btree (public_api_external_id);

CREATE INDEX ix_pharmonline_public_api_admissions_tenant_id ON public.pharmonline_public_api_identity_admissions USING btree (tenant_id);

CREATE INDEX ix_pharmonline_public_api_catalog_baselines_created_at ON public.pharmonline_public_api_catalog_baselines USING btree (created_at);

CREATE INDEX ix_pharmonline_public_api_catalog_baselines_tenant_id ON public.pharmonline_public_api_catalog_baselines USING btree (tenant_id);

CREATE INDEX ix_pharmonline_public_api_quarantines_created_at ON public.pharmonline_public_api_identity_quarantines USING btree (created_at);

CREATE INDEX ix_pharmonline_public_api_quarantines_legacy_product_id ON public.pharmonline_public_api_identity_quarantines USING btree (legacy_product_id);

CREATE INDEX ix_pharmonline_public_api_quarantines_public_id ON public.pharmonline_public_api_identity_quarantines USING btree (public_api_external_id);

CREATE INDEX ix_pharmonline_public_api_quarantines_replacement_product_id ON public.pharmonline_public_api_identity_quarantines USING btree (replacement_product_id);

CREATE INDEX ix_pharmonline_public_api_quarantines_tenant_id ON public.pharmonline_public_api_identity_quarantines USING btree (tenant_id);

CREATE INDEX ix_pharmonline_public_api_reconciliations_created_at ON public.pharmonline_public_api_identity_reconciliations USING btree (created_at);

CREATE INDEX ix_pharmonline_public_api_reconciliations_product_id ON public.pharmonline_public_api_identity_reconciliations USING btree (product_id);

CREATE INDEX ix_pharmonline_public_api_reconciliations_public_id ON public.pharmonline_public_api_identity_reconciliations USING btree (public_api_external_id);

CREATE INDEX ix_pharmonline_public_api_reconciliations_tenant_id ON public.pharmonline_public_api_identity_reconciliations USING btree (tenant_id);

CREATE INDEX ix_price_snapshots_product_id ON public.price_snapshots USING btree (product_id);

CREATE INDEX ix_price_snapshots_run_id ON public.price_snapshots USING btree (run_id);

CREATE UNIQUE INDEX ix_pricing_config_tenant_id ON public.pricing_config USING btree (tenant_id);

CREATE INDEX ix_products_availability_run_id ON public.products USING btree (availability_run_id);

CREATE INDEX ix_products_barcode ON public.products USING btree (barcode);

CREATE INDEX ix_products_brand ON public.products USING btree (brand);

CREATE INDEX ix_products_canonical_id ON public.products USING btree (canonical_id);

CREATE INDEX ix_products_category ON public.products USING btree (category);

CREATE INDEX ix_products_country_resolution_status ON public.products USING btree (country_resolution_status);

CREATE INDEX ix_products_external_id ON public.products USING btree (external_id);

CREATE INDEX ix_products_manual_category_key ON public.products USING btree (manual_category_key);

CREATE INDEX ix_products_manufacturer_country_code ON public.products USING btree (manufacturer_country_code);

CREATE INDEX ix_products_name ON public.products USING btree (name);

CREATE INDEX ix_products_name_normalized ON public.products USING btree (name_normalized);

CREATE INDEX ix_products_offer_availability_status ON public.products USING btree (offer_availability_status);

CREATE INDEX ix_products_site ON public.products USING btree (site);

CREATE INDEX ix_products_tenant_id ON public.products USING btree (tenant_id);

CREATE INDEX ix_promos_run_id ON public.promos USING btree (run_id);

CREATE INDEX ix_promos_site ON public.promos USING btree (site);

CREATE INDEX ix_promos_tenant_id ON public.promos USING btree (tenant_id);

CREATE UNIQUE INDEX ix_recipients_email ON public.recipients USING btree (email);

CREATE INDEX ix_recipients_telegram_chat_id ON public.recipients USING btree (telegram_chat_id);

CREATE INDEX ix_roi_actions_cache_computed_at ON public.roi_actions_cache USING btree (computed_at);

CREATE INDEX ix_roi_actions_cache_tenant_id ON public.roi_actions_cache USING btree (tenant_id);

CREATE INDEX ix_runs_catalog_scope ON public.runs USING btree (catalog_scope);

CREATE INDEX ix_runs_catalog_verified ON public.runs USING btree (catalog_verified);

CREATE INDEX ix_runs_tenant_id ON public.runs USING btree (tenant_id);

CREATE INDEX ix_saved_views_tenant_id ON public.saved_views USING btree (tenant_id);

CREATE INDEX ix_scrape_requests_requested_at ON public.scrape_requests USING btree (requested_at);

CREATE INDEX ix_scrape_requests_status ON public.scrape_requests USING btree (status);

CREATE INDEX ix_scrape_requests_tenant_id ON public.scrape_requests USING btree (tenant_id);

CREATE INDEX ix_stock_levels_canonical_id ON public.stock_levels USING btree (canonical_id);

CREATE INDEX ix_stock_levels_product_id ON public.stock_levels USING btree (product_id);

CREATE INDEX ix_stock_levels_sku ON public.stock_levels USING btree (sku);

CREATE INDEX ix_supplier_prices_canonical_id ON public.supplier_prices USING btree (canonical_id);

CREATE INDEX ix_supplier_prices_product_id ON public.supplier_prices USING btree (product_id);

CREATE INDEX ix_supplier_prices_sku ON public.supplier_prices USING btree (sku);

CREATE INDEX ix_tenant_users_email ON public.tenant_users USING btree (email);

CREATE INDEX ix_tenant_users_telegram_chat_id ON public.tenant_users USING btree (telegram_chat_id);

CREATE INDEX ix_tenant_users_tenant_id ON public.tenant_users USING btree (tenant_id);

CREATE UNIQUE INDEX ix_tenants_slug ON public.tenants USING btree (slug);

CREATE INDEX ix_tracked_categories_category_id ON public.tracked_categories USING btree (category_id);

CREATE INDEX ix_tracked_categories_tenant_id ON public.tracked_categories USING btree (tenant_id);

CREATE INDEX ix_tracked_product_links_tracked_product_id ON public.tracked_product_links USING btree (tracked_product_id);

CREATE INDEX ix_tracked_products_tenant_id ON public.tracked_products USING btree (tenant_id);

ALTER TABLE ONLY public.alert_events
    ADD CONSTRAINT alert_events_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.alert_rules(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.tenant_users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.cost_import_batches
    ADD CONSTRAINT cost_import_batches_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.tenant_users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.cost_import_batches
    ADD CONSTRAINT cost_import_batches_rolled_back_by_user_id_fkey FOREIGN KEY (rolled_back_by_user_id) REFERENCES public.tenant_users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.match_rejections
    ADD CONSTRAINT match_rejections_product_a_id_fkey FOREIGN KEY (product_a_id) REFERENCES public.products(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.match_rejections
    ADD CONSTRAINT match_rejections_product_b_id_fkey FOREIGN KEY (product_b_id) REFERENCES public.products(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.offer_observations
    ADD CONSTRAINT offer_observations_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.offer_observations
    ADD CONSTRAINT offer_observations_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.pharmonline_public_api_identity_admissions
    ADD CONSTRAINT pharmonline_public_api_identity_admissions_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.pharmonline_public_api_identity_quarantines
    ADD CONSTRAINT pharmonline_public_api_identity_qua_replacement_product_id_fkey FOREIGN KEY (replacement_product_id) REFERENCES public.products(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.pharmonline_public_api_identity_quarantines
    ADD CONSTRAINT pharmonline_public_api_identity_quaranti_legacy_product_id_fkey FOREIGN KEY (legacy_product_id) REFERENCES public.products(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.pharmonline_public_api_identity_reconciliations
    ADD CONSTRAINT pharmonline_public_api_identity_reconciliations_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.price_snapshots
    ADD CONSTRAINT price_snapshots_confirmed_run_id_fkey FOREIGN KEY (confirmed_run_id) REFERENCES public.runs(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.price_snapshots
    ADD CONSTRAINT price_snapshots_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id);

ALTER TABLE ONLY public.price_snapshots
    ADD CONSTRAINT price_snapshots_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id);

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_canonical_id_fkey FOREIGN KEY (canonical_id) REFERENCES public.matches(id);

ALTER TABLE ONLY public.promos
    ADD CONSTRAINT promos_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id);

ALTER TABLE ONLY public.scrape_requests
    ADD CONSTRAINT scrape_requests_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.scrape_requests
    ADD CONSTRAINT scrape_requests_requested_by_user_id_fkey FOREIGN KEY (requested_by_user_id) REFERENCES public.tenant_users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.scrape_requests
    ADD CONSTRAINT scrape_requests_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.stock_levels
    ADD CONSTRAINT stock_levels_canonical_id_fkey FOREIGN KEY (canonical_id) REFERENCES public.matches(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.stock_levels
    ADD CONSTRAINT stock_levels_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.supplier_prices
    ADD CONSTRAINT supplier_prices_canonical_id_fkey FOREIGN KEY (canonical_id) REFERENCES public.matches(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.supplier_prices
    ADD CONSTRAINT supplier_prices_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tenant_users
    ADD CONSTRAINT tenant_users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tracked_categories
    ADD CONSTRAINT tracked_categories_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.categories(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tracked_product_links
    ADD CONSTRAINT tracked_product_links_tracked_product_id_fkey FOREIGN KEY (tracked_product_id) REFERENCES public.tracked_products(id) ON DELETE CASCADE;


INSERT INTO public.alembic_version (version_num) VALUES ('0023_snapshot_confirmed_run');
