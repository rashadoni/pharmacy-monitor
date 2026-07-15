from src.category_taxonomy import classify_source_category


def _key(site: str, slug: str, label_az: str | None = None) -> str | None:
    category = classify_source_category(site, slug, label_az=label_az)
    return category.key if category else None


def test_clear_source_categories_collapse_to_compact_canonical_groups() -> None:
    assert _key("pharmonline", "antihipertenziv-dermanlar") == "cardiovascular_blood"
    assert (
        _key("pharmonline", "urek-chatishmazlighi-zamani-istifade-olunan-vasiteler")
        == "cardiovascular_blood"
    )
    assert _key("pharmonline", "quru-goz-sindromu-zamani-oftalmoprotektorlar") == "eye_health"
    assert _key("pharmonline", "dish-mecunlar") == "oral_care"
    assert _key("pharmonline", "ushaq-qidasi") == "mother_baby"
    assert (
        _key("aptekonline", "146", "Hipertoniya zamanı istifadə olunan vasitələr")
        == "cardiovascular_blood"
    )
    assert _key("aptekonline", "252", "Uşaq qidaları") == "mother_baby"


def test_broad_and_dosage_form_categories_do_not_create_dashboard_noise() -> None:
    assert _key("aloe", "dermanlar") is None
    assert _key("aloe", "tibbi-vasitələr") is None
    assert _key("pharmonline", "shamlar-3") is None
    assert _key("pharmonline", "inyeksiyalar-8") is None
    assert _key("pharmonline", "xarici-vasiteler") is None
    assert _key("pharmonline", "yerli-vasiteler-4") is None


def test_medical_skin_care_and_dermatology_stay_separate() -> None:
    assert _key("pharmonline", "deri-xestelikleri") == "dermatology"
    assert _key("pharmonline", "deriye-qulluq-vasiteler") == "personal_care"
