from __future__ import annotations

from tracking.adapters.base import SeedResolution
from tracking.services import niche_service


def _seed(*, title: str, description: str, handle: str | None = None) -> SeedResolution:
    return SeedResolution(
        platform="youtube",
        external_id="seed",
        handle=handle,
        url="https://www.youtube.com/channel/seed",
        title=title,
        description=description,
        uploads_playlist_id="uploads",
        subscriber_count=1000,
    )


def test_derive_topic_phrases_for_engineering_channel() -> None:
    text = """
    Former NASA engineer building science experiments and robotics projects.
    DIY machines, mechanical engineering and maker builds every month.
    """

    phrases = niche_service._derive_topic_phrases(text)

    assert "engineering projects" in phrases
    assert "science experiments" in phrases
    assert "robotics" in phrases


def test_infer_niche_keywords_merges_topic_phrases_when_youtube_auto_is_weak(monkeypatch) -> None:
    seed = _seed(
        title="Mark Rober",
        description="Former NASA engineer building science experiments and mechanical projects.",
        handle="markrober",
    )

    monkeypatch.setattr(niche_service, "infer_youtube_keywords", lambda s: ["engineer", "nasa", "secret", "monthly"])
    monkeypatch.setattr(
        niche_service,
        "get_recent_video_titles",
        lambda s, n=10: [
            "Engineers vs Junkyard RC Car Death Match",
            "Why MRI Machines Are So Dangerous",
            "How Close To Beat My Goalie Robot?",
        ],
    )

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[])

    assert source == "auto"
    assert "engineering projects" in keywords
    assert "science experiments" in keywords
    assert niche_service.is_niche_keywords_poor(keywords=keywords, seed=seed) is False


def test_is_niche_keywords_poor_allows_rich_phrase_keywords_for_personal_brand_seed() -> None:
    seed = _seed(
        title="Mark Rober",
        description="Former NASA engineer building science experiments and mechanical projects.",
        handle="markrober",
    )

    keywords = [
        "engineering projects",
        "science experiments",
        "robotics",
        "DIY projects",
        "mechanics",
        "crunchlabs",
    ]

    assert niche_service.is_niche_keywords_poor(keywords=keywords, seed=seed) is False


def test_infer_niche_keywords_adds_language_learning_phrases(monkeypatch) -> None:
    seed = _seed(
        title="English Teacher",
        description="Уроки английского языка и разговорный английский для начинающих.",
        handle="englishteacher",
    )

    monkeypatch.setattr(niche_service, "infer_youtube_keywords", lambda s: ["английский", "уроки", "speaking"])
    monkeypatch.setattr(
        niche_service,
        "get_recent_video_titles",
        lambda s, n=10: [
            "Разговорный английский для начинающих",
            "English speaking practice",
        ],
    )

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[])

    assert source == "auto"
    assert "английский язык" in keywords
    assert "уроки английского" in keywords
