import json

import pytest

from tiktok_categorizer.categories import (
    DEFAULT_CATEGORIES,
    UNCATEGORIZED,
    CategoryConfigError,
    KeywordClassifier,
    load_categories,
    parse_categories,
)


@pytest.fixture(scope="module")
def classify():
    return KeywordClassifier(load_categories(None)).classify


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("fall fit check 🍂 #ootd #fashiontiktok", "Fashion"),
        ("3 ways to style a white tee #outfitinspo", "Fashion"),
        ("Please help my neighbor rebuild after the fire, link to the GoFundMe in bio", "Fundraisers & Causes"),
        ("#capcutedit transition tutorial", "Cool Edits"),
        ("insane velocity edit 🔥 #edit #fyp", "Cool Edits"),
        ("POV: your boss schedules a meeting at 4:59 #corporatetiktok", "Work & Career"),
        ("how to answer 'tell me about yourself' in a job interview", "Work & Career"),
        ("easy weeknight dinner ideas #recipe", "Food & Recipes"),
        ("my dog when I say walk #dogsoftiktok", "Pets & Animals"),
    ],
)
def test_default_categories_catch_common_captions(classify, caption, expected):
    assert classify(caption)[0] == expected


def test_edit_note_in_a_caption_is_not_a_cool_edit(classify):
    assert "Cool Edits" not in classify("Edit: thank you for 1M views!! #fyp")


def test_longest_hashtag_term_wins(classify):
    labels = classify("finally fixed it #badcredit #credit")
    assert "Money & Finance" in labels
    assert "Cool Edits" not in labels


def test_video_can_belong_to_several_categories(classify):
    labels = classify("thrift haul for charity, every dollar is donated to the shelter #thrifting #fundraiser")
    assert set(labels[:2]) == {"Fashion", "Fundraisers & Causes"}


def test_nothing_recognisable_is_uncategorized(classify):
    assert classify("") == [UNCATEGORIZED]
    assert classify("lol #fyp #viral") == [UNCATEGORIZED]


def test_custom_categories_file(tmp_path):
    path = tmp_path / "mine.json"
    path.write_text(json.dumps({"categories": [
        {"name": "Poke Ideas", "keywords": ["poke bowl", "poke"], "hashtags": ["pokebowl"]},
        {"name": "Sneakers", "keywords": ["jordans"]},
    ]}))
    classify = KeywordClassifier(load_categories(path)).classify
    assert classify("best poke bowl in LA") == ["Poke Ideas"]
    assert classify("#pokebowlrecipe") == ["Poke Ideas"]
    assert classify("new Jordans!") == ["Sneakers"]
    assert classify("#ootd") == [UNCATEGORIZED]


def test_default_categories_round_trip_through_json():
    assert parse_categories(json.loads(json.dumps({"categories": DEFAULT_CATEGORIES})))


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"keywords": ["x"]}],
        [{"name": "A"}, {"name": "a"}],
        [{"name": "Uncategorized"}],
        [{"name": "A", "keywords": "not a list"}],
    ],
)
def test_bad_category_files_are_rejected(data):
    with pytest.raises(CategoryConfigError):
        parse_categories(data)


def test_invalid_json_is_reported(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    with pytest.raises(CategoryConfigError, match="not valid JSON"):
        load_categories(path)
