import json

import pytest

from scripts.load_categories_from_map import (
    filter_discovered_categories,
    load_excluded_slugs,
)


def test_repository_category_exclusions_cover_confirmed_zero_slugs():
    excluded = load_excluded_slugs()

    assert excluded == {
        ("pharmonline", "arabalar"),
        ("aptekonline", "308"),
        ("aptekonline", "374"),
        ("aptekonline", "419"),
        ("aptekonline", "425"),
    }


def test_category_exclusions_fail_closed_on_malformed_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1, "excluded": [{"site": "aloe"}]}))

    with pytest.raises(ValueError, match="invalid category exclusion row"):
        load_excluded_slugs(path)


def test_regenerated_map_cannot_reintroduce_tombstoned_slugs():
    raw = [
        {"site": "aptekonline", "slug": "308", "name": "Narkoz"},
        {"site": "pharmonline", "slug": "arabalar", "name": "Arabalar"},
        {"site": "aptekonline", "slug": "341", "name": "Cərrahi"},
    ]

    filtered, excluded_count = filter_discovered_categories(
        raw,
        load_excluded_slugs(),
    )

    assert excluded_count == 2
    assert [(row["site"], row["slug"]) for row in filtered] == [
        ("aptekonline", "341")
    ]
