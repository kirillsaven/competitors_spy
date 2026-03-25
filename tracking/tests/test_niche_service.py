from __future__ import annotations

from tracking.adapters.base import SeedResolution
from tracking.models import Platform
from tracking.services import niche_service


def test_build_keyword_source_text_omits_structural_metadata(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: ["playoff race analysis", "basketball highlights"],
    )
    seed = SeedResolution(
        platform=Platform.TIKTOK,
        external_id="tt-1",
        handle="nba",
        url="https://www.tiktok.com/@nba",
        title="NBA",
        description="Daily basketball clips",
        uploads_playlist_id=None,
    )

    text = niche_service.build_keyword_source_text(seed=seed, competitors=[])

    assert "Platform:" not in text
    assert "Handle:" not in text
    assert "URL:" not in text
    assert "https://www.tiktok.com/@nba" not in text
    assert "Daily basketball clips" in text
    assert "playoff race analysis" in text


def test_infer_niche_keywords_auto_uses_recent_content_not_structure(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: ["streetwear drops", "sneaker styling", "fashion lookbook"],
    )
    seed = SeedResolution(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="fitsdaily",
        url="https://www.instagram.com/fitsdaily/",
        title="Fits Daily",
        description="outfit inspiration and streetwear",
        uploads_playlist_id=None,
    )

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[], prefer_llm=False)

    assert source == "auto"
    assert "platform" not in keywords
    assert "handle" not in keywords
    assert "url" not in keywords
    assert "streetwear" in keywords
