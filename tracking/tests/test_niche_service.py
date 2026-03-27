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


def test_build_keyword_source_text_combines_confirmed_linked_accounts(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: {
            Platform.YOUTUBE: ["rocket launch recap"],
            Platform.TIKTOK: ["space explainer clips"],
            Platform.INSTAGRAM: ["mars photo breakdown"],
        }.get(seed.platform, []),
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="nasa",
        url="https://www.youtube.com/@nasa",
        title="NASA",
        description="space exploration",
        uploads_playlist_id="UU123",
    )
    linked_accounts = [
        seed,
        SeedResolution(
            platform=Platform.TIKTOK,
            external_id="tt-1",
            handle="nasa",
            url="https://www.tiktok.com/@nasa",
            title="NASA TikTok",
            description="short science explainers",
            uploads_playlist_id=None,
        ),
        SeedResolution(
            platform=Platform.INSTAGRAM,
            external_id="ig-1",
            handle="nasa",
            url="https://www.instagram.com/nasa/",
            title="NASA Instagram",
            description="space photography",
            uploads_playlist_id=None,
        ),
    ]

    text = niche_service.build_keyword_source_text(seed=seed, competitors=[], linked_accounts=linked_accounts)

    assert "space exploration" in text
    assert "short science explainers" in text
    assert "space photography" in text
    assert "rocket launch recap" in text
    assert "space explainer clips" in text
    assert "mars photo breakdown" in text
    assert "Platform:" not in text
    assert "Handle:" not in text
    assert "URL:" not in text


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
    assert any("streetwear" in keyword for keyword in keywords)
    assert any("fashion" in keyword or "sneaker" in keyword for keyword in keywords)


def test_infer_niche_keywords_uses_account_title_for_search_ready_subject_hints(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: ["Unit 1 practice", "Unit 2 listening"],
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="okenglish",
        url="https://www.youtube.com/channel/UCQQpescDpZ6d3lu9j0fPA7g",
        title="OK English - уроки английского языка",
        description="Практика английского языка для начинающих и продолжающих.",
        uploads_playlist_id="UU1",
    )

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[], prefer_llm=False)

    assert source == "auto"
    assert any("англий" in keyword for keyword in keywords)
    assert any("урок" in keyword or "english" in keyword for keyword in keywords)


def test_infer_niche_keywords_auto_filters_ru_en_junk_words(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "street food маршрут по Москве и кофейные гиды",
            "лучшие кофейные гиды и street food точки",
            "platform handle url seed title если можно просто набор",
        ],
    )
    seed = SeedResolution(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="coffeewalks",
        url="https://www.instagram.com/coffeewalks/",
        title="Coffee Walks",
        description="Просто можно если нужно. Street food обзоры и кофейные гиды.",
        uploads_playlist_id=None,
    )

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[], prefer_llm=False)

    assert source == "auto"
    assert "street food" in keywords
    assert "кофейные гиды" in keywords
    for junk in {"если", "можно", "просто", "набор", "platform", "handle", "url"}:
        assert junk not in keywords


def test_infer_niche_keywords_auto_prefers_phrase_like_teacher_topics(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "Уроки английского для преподавателей и teacher groups",
            "Материалы для репетиторов английского: tutor notes и разборы уроков",
            "Дарья Панчо: как объяснять грамматику без скуки",
            "Планы уроков английского для онлайн-репетиторов",
        ],
    )
    seed = SeedResolution(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="dariapancho",
        url="https://www.instagram.com/dariapancho/",
        title="Дарья Панчо | онлайн-репетитор | онлайн-школа английского",
        description="Заметки для преподавателей английского, teacher groups и материалы для репетиторов.",
        uploads_playlist_id=None,
    )

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[], prefer_llm=False)

    assert source == "auto"
    assert 1 <= len(keywords) <= 8
    assert all(len(keyword.split()) >= 2 for keyword in keywords)
    assert any("английск" in keyword for keyword in keywords)
    assert any("преподав" in keyword or "репетитор" in keyword for keyword in keywords)
    for banned in {"дарья", "панчо", "объяснять", "бояться", "новый", "сложных", "уровень", "рост"}:
        assert all(banned not in keyword for keyword in keywords)


def test_infer_niche_keywords_blocks_seed_identity_tokens_even_if_they_repeat_in_description(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "ЕГЭ по английскому: аудирование и письмо",
            "Как готовиться к ЕГЭ по английскому онлайн",
            "Разбор заданий ЕГЭ по английскому",
        ],
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="tanya_shibitova",
        url="https://www.youtube.com/channel/UC91u057zoN-kYmo2z7G5RZg",
        title="Таня Шибитова | Английский ЕГЭ | 100балльный",
        description="Татьяна Шибитова — преподаватель по английскому языку. Готовлю к ЕГЭ онлайн.",
        uploads_playlist_id="UU1",
    )

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[], prefer_llm=False)

    assert source == "auto"
    assert any("егэ" in keyword for keyword in keywords)
    assert any("англий" in keyword for keyword in keywords)
    for banned in {"таня", "татьяна", "шибитова", "100балльный"}:
        assert all(banned not in keyword for keyword in keywords)


def test_infer_niche_keywords_keeps_topical_handle_words_when_supported_by_content(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "English teacher lesson plans",
            "Teacher notes for english lessons",
            "English tutor worksheets",
        ],
    )
    seed = SeedResolution(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="english_teacher",
        url="https://www.instagram.com/english_teacher/",
        title="English Teacher",
        description="English teacher notes and lesson plans",
        uploads_playlist_id=None,
    )

    keywords, _ = niche_service.infer_niche_keywords(seed=seed, competitors=[], prefer_llm=False)

    assert any("english" in keyword for keyword in keywords)
    assert any("teacher" in keyword or "lesson" in keyword for keyword in keywords)


def test_infer_niche_keywords_dedupes_same_stem_phrase_reordering(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "space news",
            "news space",
            "space news analysis",
        ],
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="space-news",
        url="https://www.youtube.com/@space-news",
        title="Space News",
        description="Space news and analysis",
        uploads_playlist_id="UU1",
    )

    keywords, _ = niche_service.infer_niche_keywords(seed=seed, competitors=[], prefer_llm=False)

    two_word_variants = [keyword for keyword in keywords if set(keyword.split()) == {"space", "news"}]
    assert len(two_word_variants) == 1


def test_infer_niche_keywords_auto_weights_repeated_topics_across_linked_accounts(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: {
            Platform.YOUTUBE: ["space news breakdown", "mars mission updates"],
            Platform.TIKTOK: ["daily space news clips", "mars mission explainer"],
            Platform.INSTAGRAM: ["space news photos", "behind the mars mission"],
        }.get(seed.platform, []),
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="orbitdesk",
        url="https://www.youtube.com/@orbitdesk",
        title="Orbit Desk",
        description="Space news and mission explainers",
        uploads_playlist_id="UU1",
    )
    linked_accounts = [
        seed,
        SeedResolution(
            platform=Platform.TIKTOK,
            external_id="tt-1",
            handle="orbitdesk",
            url="https://www.tiktok.com/@orbitdesk",
            title="Orbit Desk",
            description="Quick space news and mission explainers",
            uploads_playlist_id=None,
        ),
        SeedResolution(
            platform=Platform.INSTAGRAM,
            external_id="ig-1",
            handle="orbitdesk",
            url="https://www.instagram.com/orbitdesk/",
            title="Orbit Desk",
            description="Space news photos and launch explainers",
            uploads_playlist_id=None,
        ),
    ]

    keywords, _ = niche_service.infer_niche_keywords(
        seed=seed,
        competitors=[],
        prefer_llm=False,
        linked_accounts=linked_accounts,
    )

    assert "space news" in keywords
    assert any("mars mission" in keyword for keyword in keywords)
    assert "mission explainers" in keywords


def test_build_niche_context_text_includes_multi_platform_bundle(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [f"{seed.platform}-recent"],
    )
    seed = SeedResolution(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="creator",
        url="https://www.instagram.com/creator/",
        title="Creator",
        description="style and fashion",
        uploads_playlist_id=None,
    )
    linked_accounts = [
        seed,
        SeedResolution(
            platform=Platform.TIKTOK,
            external_id="tt-1",
            handle="creator",
            url="https://www.tiktok.com/@creator",
            title="Creator",
            description="outfit clips",
            uploads_playlist_id=None,
        ),
    ]

    text = niche_service.build_niche_context_text(seed=seed, competitors=[], linked_accounts=linked_accounts)

    assert "CONFIRMED USER ACCOUNTS" in text
    assert "Platform: instagram" in text
    assert "Platform: tiktok" in text
    assert "- instagram-recent" in text
    assert "- tiktok-recent" in text
