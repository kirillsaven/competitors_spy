from __future__ import annotations

from tracking.services.report_filters import (
    content_matches_stopwords,
    normalize_stopword,
    normalize_stopwords,
    parse_stopwords_input,
)


def test_normalize_stopwords_collapses_spaces_and_deduplicates_case_insensitively():
    assert normalize_stopword("  Foo   Bar  ") == "foo bar"
    assert normalize_stopwords(["  Foo   Bar  ", "foo bar", "BAZ", " baz "]) == ["foo bar", "baz"]


def test_parse_stopwords_input_supports_lines_and_commas():
    assert parse_stopwords_input("  Foo bar,\nBAZ,\nfoo   bar ") == ["foo bar", "baz"]


def test_content_matches_stopwords_uses_normalized_title_and_description():
    assert content_matches_stopwords(
        title="  Major   Launch  ",
        description="New COURSE is here",
        stopwords=["launch new", "course"],
    )
    assert not content_matches_stopwords(
        title="Weekly recap",
        description="Channel update",
        stopwords=["course", "promo"],
    )
