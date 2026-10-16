"""Precision/recall регрессия guard-слоя матчера (аудит L8: 15 guard'ов тюнились
на глаз, без метрики; одна регрессия уже была — c252211 equal-peer gate).

Лейбл-сет: пары ПОХОЖИХ товаров (одна линейка/бренд — т.е. попали бы в один bucket)
с человеко-меткой `match`. Меряем guard-слой:
  - POSITIVE класс для guard'а = «РАЗНЫЕ товары» (guard должен заблокировать).
  - FP (over-block) = guard заблокировал ОДИН товар → СЛОМАЛ бы матч (худшая ошибка
    для price-comparison). Требуем FP == 0.
  - FN (under-block) = guard пропустил РАЗНЫЕ → ложный матч. Требуем FN == 0 на
    курируемом сете (все кейсы, под которые guard'ы и писались).

Любой будущий guard-чейндж, ломающий эти кейсы, валит тест → регрессия поймана в CI.
Расширять сет новыми кейсами при каждом новом классе ложных матчей.
"""

from types import SimpleNamespace

from src.matcher import (
    _has_conflicting_concentration,
    _has_conflicting_country,
    _has_conflicting_dimensions,
    _has_conflicting_dose,
    _has_conflicting_form,
    _has_conflicting_gender,
    _has_conflicting_modifier,
    _has_conflicting_orphan_number,
    _has_conflicting_pack_volume,
    _has_conflicting_route,
    _has_conflicting_series_number,
    _has_conflicting_strength_number,
    _has_conflicting_variant_atoms,
    _has_conflicting_variant_marker,
    _has_conflicting_variant_tokens,
)
from src.normalize import normalize_name


def _blocks(a_name, b_name, a_mfr=None, b_mfr=None, a_url="", b_url=""):
    """True если ХОТЬ ОДИН guard блокирует пару (как все проходы матчера вместе)."""
    a = SimpleNamespace(
        name=a_name,
        name_normalized=normalize_name(a_name),
        site="aptekonline",
        manufacturer=a_mfr,
        url=a_url,
        manufacturer_country_code=("tr" if a_mfr else None),
        country_resolution_status=("resolved" if a_mfr else "unknown"),
    )
    b = SimpleNamespace(
        name=b_name,
        name_normalized=normalize_name(b_name),
        site="pharmonline",
        manufacturer=b_mfr,
        url=b_url,
        manufacturer_country_code=("az" if b_mfr else None),
        country_resolution_status=("resolved" if b_mfr else "unknown"),
    )
    an, bn, ar, br = a.name_normalized, b.name_normalized, a.name, b.name
    return (
        _has_conflicting_form(ar, br)
        or _has_conflicting_route(ar, br)
        or _has_conflicting_gender(an, bn)
        or _has_conflicting_series_number(an, bn)
        # как в проходах матчера: с исходными названиями
        or _has_conflicting_orphan_number(an, bn, ar, br)
        or _has_conflicting_modifier(an, bn)
        or _has_conflicting_dose(a, b)
        or _has_conflicting_pack_volume(a, b)
        or _has_conflicting_variant_tokens(an, bn)
        or _has_conflicting_variant_atoms(ar, br)
        or _has_conflicting_strength_number(ar, br)
        or _has_conflicting_variant_marker(ar, br)
        or _has_conflicting_country(a, b)
        or _has_conflicting_dimensions(ar, br)
        or _has_conflicting_concentration(ar, br)
    )


# (a, b, match?, note). match=True → ОДИН товар (guard НЕ должен блокировать).
_LABELED: list[tuple] = [
    # ── POSITIVE: один товар, verbose-vs-terse / разная фасовка — НЕ блокировать ──
    ("Spiral Yunona Bio-T Ag", 'Spiral "Yunona" Bio-T  Ag', True, "same silver"),
    ("Novalans  30 mq N14", "Novalans 30 mq № 14 (Kapsulalar)", True, "verbose form"),
    ("Kapsikam məlhəm  30 q", "Kapsikam 30 qr (Məlhəm)", True, "verbose form"),
    ("Aspirin 500 N20", "Aspirin 500 mq № 20", True, "same pack, verbose dosage"),
    ("Amoxicillin 500 N20", "Amoxicillin trihydrate 500 N20", True, "verbose name"),
    ("Duspatalin 200 mq N30", "Duspatalin 200 mq № 30 (Həb)", True, "verbose form"),
    ("Bio-T Cu380 type1", 'Spiral "Yunona" Bio-T Cu380 type1', True, "same type1"),
    # 2026-10-07: один товар в разной записи на трёх сайтах (жалоба клиента на
    # «не находит kreon / veqovi / ozempik» и замер на копии каталога).
    ("Kreon 10000  N20", "Kreon 10000 №20 (Kapsula)", True, "strength kept on both"),
    ("Creon 10000 20 əd.", "Kreon 10000  N20", True, "c/k spelling"),
    ("Orniksil  N30 (Ornicsil)", "Orniksil № 30 (Tabletlər) (Latviya)", True, "latin twin"),
    ("Sitramon P  N6", "Sitramon-P № 6 (Tabletlər)", True, "hyphen"),
    ("Xalatan 0,005% 2,5 ml", "Ksalatan  0.005%  2.5 ml (göz damcısı)", True, "comma"),
    (
        "Lukastin Plus  5 mq/ 10 mq  N30",
        "Lukastin plus (10+5)mq № 30(Tabletlər)",
        True,
        "dose list with one unit",
    ),
    ("Euthyrox 100 mkq 100 əd.", "Eutiroks 100 mkg № 100", True, "mkq = mkg"),
    (
        "Pulmares  0.25 mq/ml 2 ml N20 (inhalyasiya üçün suspenziya)",
        "Pulmares 0.25 mq/ml 2 ml №20 (Flakon)",
        True,
        "form vs container note",
    ),
    (
        "Veqovi   0.25 mq/doza   1 mq  1.5 ml   N1  (Wegovy)",
        "Veqovi 0.25 mq (0.68 mq/ml) 1,5 ml № 1",
        True,
        "per-dose vs concentration note",
    ),
    (
        "Ozempik  0.25 mq, 0.5 mq/doza  N1 (məhlullu şpris-qələm)",
        "Ozempik 0.25, 0.5 mq/doza 1.5 ml № 1 (Şpris -qələm)",
        True,
        "pen with two doses",
    ),
    ("Siqnum  50 mq  2 ml  N6 (Signum)", "Siqnum 50 mq / 2 ml № 6", True, "per-ampoule ml"),
    ("Lorstop  30 ml (oral sprey)", "Lorstop 30 ml (Sprey boğaz üçün)", True, "mouth=throat"),
    # ── NEGATIVE: РАЗНЫЕ варианты/спеки — guard ДОЛЖЕН блокировать ──
    ("Spiral Yunona Bio-T Ag", 'Spiral "Yunona" Bio-T Tip 1', False, "silver vs type"),
    ("Bio-T Cu380 type1", "Bio-T Cu380 type2", False, "type1 vs type2"),
    ("Spiral Bio-T Ag", "Spiral Bio-T Cu380", False, "silver vs copper"),
    ("Lorinden C məlhəm 15 q", "Lorinden A 15 qr", False, "variant atom C/A"),
    ("Vitamin A № 10", "Vitamin C 100 mq N10", False, "variant atom A/C"),
    ("Tetrasiklin 3% 15q", "Tetrasiklin 1% 15q", False, "concentration 3%/1%"),
    ("Alban 10 sm x 10 sm N25", "Alban 10 sm x 25 sm N25", False, "dimension 10/25"),
    ("Mikrazim 25000 ED N20", "Mikrazim 10000 ED N20", False, "strength 25000/10000"),
    ("Nutrilon 1 400q", "Nutrilon 4 400q", False, "series 1/4"),
    ("Huggies oğlanlar N64", "Huggies qızlar N64", False, "gender male/female"),
    ("Kreon 10000  N20", "Kreon 25000 №20 (Kapsula)", False, "strength 10000/25000"),
    ("Aspirin 500 N20", "Aspirin 250 mq № 20", False, "bare 500 vs 250 mq"),
    ("Otipaks 15 ml (qulaq damcısı)", "Otipaks 15 ml (göz damcısı)", False, "ear vs eye"),
    ("Dolpan   75 mq  2 ml  N10", "Dolpan 75 mq/3 ml № 10 (Ampulalar)", False, "2 ml vs 3 ml"),
    (
        "Veqovi   1 mq/doza   4 mq  3 ml   N1  (Wegovy)",
        "Veqovi 0.25 mq (0.68 mq/ml) 1,5 ml № 1",
        False,
        "pen dose 1 vs 0.25",
    ),
    ("Tripliksam 5 mq/1.25 mq/10 mq N30", "Tripliksam (5+1.25+5) mq № 30", False, "combo"),
    (
        "Gliserin Talya",
        "Gliserin Azerfarm",
        False,
        "country TR/AZ",
    ),
]


def test_guard_layer_no_overblock_no_miss():
    fp, fn = [], []
    for row in _LABELED:
        a, b, match = row[0], row[1], row[2]
        note = row[3] if len(row) > 3 else ""
        a_mfr = b_mfr = None
        # country кейс: задаём страну-производителя явно
        if "country" in note:
            a_mfr, b_mfr = "Türkiyə", "Azərbaycan"
        blocked = _blocks(a, b, a_mfr=a_mfr, b_mfr=b_mfr)
        if match and blocked:
            fp.append((a, b, note))  # over-block — сломали бы матч
        elif not match and not blocked:
            fn.append((a, b, note))  # under-block — ложный матч
    tot_neg = sum(1 for r in _LABELED if not r[2])
    tot_pos = sum(1 for r in _LABELED if r[2])
    precision = (tot_neg - len(fp)) / max(tot_neg - len(fp) + len(fp), 1)
    recall = (tot_neg - len(fn)) / max(tot_neg, 1)
    msg = (
        f"\nguard P≈{precision:.2f} R≈{recall:.2f} | pos={tot_pos} neg={tot_neg}"
        f"\nOVER-BLOCK (FP, сломали бы матч): {fp}"
        f"\nUNDER-BLOCK (FN, ложный матч): {fn}"
    )
    assert not fp, "guard over-blocks одинаковые товары:" + msg
    assert not fn, "guard пропускает разные товары:" + msg
