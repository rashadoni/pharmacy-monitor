import random

import pytest

import src.category_taxonomy as taxonomy
from src.category_taxonomy import (
    classify_source_category,
    classify_source_category_detailed,
    rule_validation_problems,
)


def _key(
    site: str, slug: str, label_az: str | None = None, label_ru: str | None = None
) -> str | None:
    category = classify_source_category(site, slug, label_az=label_az, label_ru=label_ru)
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
    assert _key("pharmonline", "peroral-vasiteler-tabletler-kapsullar-12") is None
    assert _key("pharmonline", "inyeksiyalar-ve-infuziyalar") is None
    assert _key("pharmonline", "guclu-tesiredici-vasiteler") is None


def test_medical_skin_care_and_dermatology_stay_separate() -> None:
    assert _key("pharmonline", "deri-xestelikleri") == "dermatology"
    assert _key("pharmonline", "deriye-qulluq-vasiteler") == "personal_care"


# ─── Static rule-set health ──────────────────────────────────────────────────


def test_rule_set_has_no_static_defects() -> None:
    """A duplicate phrase across two groups would make results order-dependent."""
    assert rule_validation_problems() == []


def test_validation_flags_a_phrase_shared_by_two_canonical_groups() -> None:
    rules = (
        taxonomy.Rule("a.dup", "nervous_system", "beyin qan"),
        taxonomy.Rule("b.dup", "cardiovascular_blood", "beyin qan"),
    )
    problems = taxonomy._validate_rules(rules)
    assert any("multiple canonical keys" in p for p in problems)


def test_validation_flags_a_phrase_that_is_not_in_folded_form() -> None:
    # `shampun` can never match: real text folds the digraph to `sampun`.
    problems = taxonomy._validate_rules((taxonomy.Rule("x", "personal_care", "shampun"),))
    assert any("canonical folded form" in p for p in problems)


# ─── Order independence (the core contract) ──────────────────────────────────


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_classification_is_independent_of_rule_order(monkeypatch, seed) -> None:
    cases = [
        ("pharmonline", "respirator-distress-sindromu"),
        ("pharmonline", "beyin-qan-dovrani-uchun-vasiteler"),
        ("pharmonline", "dish-mecunlar"),
        ("pharmonline", "ushaq-derisine-qulluq"),
        ("pharmonline", "antihipertenziv-dermanlar"),
    ]
    baseline = [_key(site, slug) for site, slug in cases]

    shuffled = list(taxonomy._RULES)
    random.Random(seed).shuffle(shuffled)
    monkeypatch.setattr(taxonomy, "_RULES", tuple(shuffled))
    assert [_key(site, slug) for site, slug in cases] == baseline

    monkeypatch.setattr(taxonomy, "_RULES", tuple(reversed(taxonomy._RULES)))
    assert [_key(site, slug) for site, slug in cases] == baseline


# ─── Regressions for defects proven against production data (2026-07-15) ─────


def test_respirator_distress_is_a_disease_not_a_device() -> None:
    """Generic `respirator` (mask) must not swallow the disease bucket."""
    assert _key("pharmonline", "respirator-distress-sindromu") == "respiratory_ent"


def test_plain_respirator_still_reads_as_a_device() -> None:
    assert _key("aptekonline", "318", "Tibbi maskalar və respiratorlar") == "medical_devices"


def test_az_digraph_and_diacritic_spellings_classify_identically() -> None:
    """PharmOnline slugs use `sh`; AptekOnline labels use `ş`. Same concept."""
    assert _key("pharmonline", "shampunlar") == "personal_care"
    assert _key("aptekonline", "271", "Şampunlar") == "personal_care"
    assert _key("aptekonline", "271", "Sampunlar") == "personal_care"


def test_immunodeficiency_maps_to_infections_and_immunity() -> None:
    """The old `immuncatish` signal could never match real text."""
    assert (
        _key("aptekonline", "240", "İmmun çatışmazlığı zamanı istifadə olunan vasitələr")
        == "infectious_immune"
    )


def test_bad_supplements_signal_does_not_fire_inside_unrelated_words() -> None:
    assert _key("aloe", "bad") == "vitamins_supplements"
    # `badam` = almond. A prefix match would misfile it as a supplement bucket.
    assert _key("pharmonline", "badam-yagi") != "vitamins_supplements"


def test_cerebral_circulation_resolves_deterministically_to_vascular() -> None:
    assert _key("pharmonline", "beyin-qan-dovrani-uchun-vasiteler") == "cardiovascular_blood"


# ─── Documented segment policy ───────────────────────────────────────────────


def test_kids_buckets_are_reported_under_the_segment_consistently() -> None:
    """Every kids bucket lands in one place, whatever else also fires."""
    assert _key("aptekonline", "361", "Uşaqlar üçün ağız boşluğuna qulluq") == "mother_baby"
    assert _key("aptekonline", "80", "Uşaq dərisinə qulluq vasitələri") == "mother_baby"
    assert _key("aptekonline", "395", "Uşaqlar üçün optik çərçivələr") == "mother_baby"
    assert _key("pharmonline", "ushaq-sachlari-uchun-qulluq") == "mother_baby"


def test_pregnancy_signals_do_not_inherit_the_kids_segment_override() -> None:
    """`hamile` also fires on "protection FROM pregnancy" — the segment's inverse.

    Keying the override on the `mother_baby` group (rather than on the kids
    signals) filed contraceptives under "Мама и ребёнок".
    """
    result = classify_source_category_detailed(
        "aptekonline", "61", label_az="Hamiləlikdən qorunma vasitələri (kontraseptivlər)"
    )
    assert result.category is None
    assert result.reason == "ambiguous"
    assert set(result.candidates) == {"mother_baby", "urogenital_reproductive"}


def test_pregnancy_bucket_still_maps_when_nothing_collides() -> None:
    assert _key("aptekonline", "60", "Hamiləlik") == "mother_baby"


def test_segment_override_is_carried_by_signals_not_by_the_group() -> None:
    overriding = {r.id for r in taxonomy._RULES if r.segment_override}
    assert overriding == {"mb.ana_usaq", "mb.usaq", "mb.korpe", "mb.baby", "mb.pediatr"}


def test_obstetric_signals_fail_closed_on_collision() -> None:
    """`dogus`/`laktasiya` are obstetric, not segment markers — pin them too."""
    lactation = classify_source_category_detailed(
        "aptekonline", "x", label_az="Laktasiya zamanı istifadə olunan vitaminlər"
    )
    assert lactation.category is None
    assert lactation.reason == "ambiguous"
    assert set(lactation.candidates) == {"mother_baby", "vitamins_supplements"}


def test_uterus_is_urogenital_not_a_kids_bucket() -> None:
    """`uşaqlıq` (uterus) folds to `usaqliq`, which starts with `usaq` (child).

    Without an explicit supersede the kids signal wins by linguistic accident
    — and, being a segment_override, wins loudly. Live on production: 2
    products under "Средства при гипертонусе матки".
    """
    assert (
        _key("aptekonline", "63", "Uşaqlığın hipertonusu zamanı istifadə olunan vasitələr")
        == "urogenital_reproductive"
    )
    assert (
        _key("aptekonline", "227", "Uşaqlıq yoluna daraldıcı təsir göstərən vasitələr")
        == "urogenital_reproductive"
    )


def test_uterus_guard_does_not_touch_any_kids_bucket() -> None:
    """`usaqli` must not be a prefix of `usaqlar`/`usaq qidasi`."""
    assert _key("aptekonline", "252", "Uşaq qidaları") == "mother_baby"
    assert _key("aptekonline", "395", "Uşaqlar üçün optik çərçivələr") == "mother_baby"
    assert _key("aptekonline", "80", "Uşaq dərisinə qulluq vasitələri") == "mother_baby"
    assert _key("pharmonline", "ushaq-bezleri") == "mother_baby"


def test_validation_rejects_segment_override_outside_the_segment_group() -> None:
    problems = taxonomy._validate_rules(
        (taxonomy.Rule("x.bad", "oral_care", "dental", segment_override=True),)
    )
    assert any("only 'mother_baby' may carry it" in p for p in problems)


def test_validation_rejects_a_supersedes_cycle() -> None:
    """A cycle passes every other check and then annihilates both signals."""
    rules = (
        taxonomy.Rule("a", "eye_health", "oftalm", supersedes=frozenset({"b"})),
        taxonomy.Rule("b", "digestive_system", "hezm", supersedes=frozenset({"a"})),
    )
    assert any("supersedes cycle" in p for p in taxonomy._validate_rules(rules))


def test_validation_rejects_self_supersede() -> None:
    rules = (taxonomy.Rule("a", "eye_health", "oftalm", supersedes=frozenset({"a"})),)
    problems = taxonomy._validate_rules(rules)
    assert any("supersedes itself" in p for p in problems)


def test_annihilated_signals_are_distinguishable_from_no_match(monkeypatch) -> None:
    """A cycle must not look identical to "no rule matched" in the audit."""
    monkeypatch.setattr(
        taxonomy,
        "_RULES",
        (
            taxonomy.Rule("a", "eye_health", "oftalm", supersedes=frozenset({"b"})),
            taxonomy.Rule("b", "digestive_system", "hezm", supersedes=frozenset({"a"})),
        ),
    )
    result = classify_source_category_detailed("pharmonline", "oftalm-ve-hezm")
    assert result.category is None
    assert result.reason == "superseded_out"
    assert result.rule_ids == ("a", "b")


def test_segment_policy_is_reported_as_such_for_audit() -> None:
    result = classify_source_category_detailed(
        "aptekonline", "361", label_az="Uşaqlar üçün ağız boşluğuna qulluq"
    )
    assert result.category is not None
    assert result.category.key == "mother_baby"
    assert result.reason == "segment_policy"
    assert set(result.candidates) == {"mother_baby", "oral_care"}


def test_adult_equivalents_are_untouched_by_the_segment_policy() -> None:
    assert _key("aptekonline", "353", "Optik çərçivələr") == "medical_devices"
    assert _key("aptekonline", "9", "Ağız boşluğuna qulluq") == "oral_care"


# ─── Fail-closed behaviour ───────────────────────────────────────────────────


def test_two_unrelated_groups_firing_together_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        taxonomy,
        "_RULES",
        (
            taxonomy.Rule("t.eye", "eye_health", "oftalm"),
            taxonomy.Rule("t.dig", "digestive_system", "hezm"),
        ),
    )
    result = classify_source_category_detailed("pharmonline", "oftalm-ve-hezm-vasiteleri")
    assert result.category is None
    assert result.reason == "ambiguous"
    assert set(result.candidates) == {"digestive_system", "eye_health"}


def test_a_phrase_cannot_span_the_seam_between_slug_and_label() -> None:
    """`goz` from a slug must not glue onto `xestelikleri` from a label."""
    result = classify_source_category_detailed("pharmonline", "goz", label_ru="Xestelikleri")
    assert result.category is None


def test_unmapped_reasons_are_distinguishable_for_audit() -> None:
    assert classify_source_category_detailed("aloe", "dermanlar").reason == "blocked_broad_bucket"
    assert (
        classify_source_category_detailed("pharmonline", "inyeksiyalar-8").reason
        == "blocked_dosage_form"
    )
    assert classify_source_category_detailed("pharmonline", "zzz-unknown").reason == "no_signal"
    assert classify_source_category_detailed("pharmonline", None).reason == "no_slug"
