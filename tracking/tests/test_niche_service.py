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

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[])

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

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[])

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

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[])

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

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[])

    assert source == "auto"
    assert 1 <= len(keywords) <= 8
    assert any("английск" in keyword for keyword in keywords)
    assert any("преподав" in keyword or "репетитор" in keyword for keyword in keywords)
    assert any(len(keyword.split()) >= 2 for keyword in keywords)
    for banned in {"дарья", "панчо", "объяснять", "бояться", "новый", "сложных", "уровень", "рост"}:
        assert all(banned not in keyword for keyword in keywords)


def test_infer_niche_keywords_expands_teacher_search_phrases_from_recent_content(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "Как начать преподавать взрослым? Оставляй заявку на занятия в группе преподавателей",
            "Что посмотреть в оригинале, если у тебя начальный уровень языка?",
            "Помогаю ученикам заговорить на английском без зубрежки",
        ],
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="dariapancho",
        url="https://www.youtube.com/@dariapancho",
        title="Daria Pancho",
        description="Онлайн-репетитор по английскому и группы для преподавателей английского.",
        uploads_playlist_id="UU1",
    )

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[])

    assert source == "auto"
    assert "английский для взрослых" in keywords
    assert "английский для начинающих" in keywords
    assert "разговорный английский" in keywords
    assert all("найди" not in keyword and "ссыл" not in keyword for keyword in keywords)


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

    keywords, source = niche_service.infer_niche_keywords(seed=seed, competitors=[])

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

    keywords, _ = niche_service.infer_niche_keywords(seed=seed, competitors=[])

    assert any("english" in keyword for keyword in keywords)
    assert any("teacher" in keyword or "lesson" in keyword for keyword in keywords)


def test_infer_niche_keywords_keeps_game_subject_from_title_and_description(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "ПУТЬ В ТОП 100 — ТОКСИЧНЫЕ РУИНЕРЫ",
            "С НУЛЯ ДО ТИТАНА — РАНГ ПСИХОВ",
            "ЗАСНАЙПИЛ ГОЛОВАЧА И ДОВЕЛ ЕГО ДО ИСТЕРИКИ",
        ],
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-dota",
        handle="pinkmandota",
        url="https://www.youtube.com/@pinkmandota",
        title="PiNKMAN DOTA",
        description="Человек, который любит проводить время с лучшими представителями мира Доты 2.",
        uploads_playlist_id="UU1",
    )

    keywords, _ = niche_service.infer_niche_keywords(seed=seed, competitors=[])

    assert any(keyword in {"dota", "dota 2"} for keyword in keywords)
    assert any("dota 2" in keyword or "dota" == keyword for keyword in keywords)
    assert all("pinkman" not in keyword for keyword in keywords)
    assert all("путь" not in keyword for keyword in keywords)
    assert all("мир" not in keyword for keyword in keywords)


def test_infer_niche_keywords_expands_dota_search_ready_phrases(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "Dota 2 guide for offlane players",
            "Разбор патча Dota 2 и метовых героев",
            "Гайд по mmr апу в dota 2",
            "Лучшие фишки для саппортов в дота 2",
        ],
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-dota",
        handle="pinkmandota",
        url="https://www.youtube.com/@pinkmandota",
        title="Pinkman Dota 2",
        description="Гайды, разборы матчапов и обучение по Dota 2.",
        uploads_playlist_id="UU1",
    )

    keywords, _ = niche_service.infer_niche_keywords(seed=seed, competitors=[])

    assert any(keyword == "dota 2" for keyword in keywords)
    assert any("гайды dota 2" in keyword or "разборы dota 2" in keyword for keyword in keywords)


def test_merge_keyword_lists_keeps_digit_qualified_topic_distinct():
    merged = niche_service._merge_keyword_lists(
        ["dota"],
        ["dota 2"],
        max_keywords=8,
    )

    assert "dota" in merged
    assert "dota 2" in merged


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

    keywords, _ = niche_service.infer_niche_keywords(seed=seed, competitors=[])

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
        linked_accounts=linked_accounts,
    )

    assert "space news" in keywords
    assert any("mars mission" in keyword for keyword in keywords)
    assert "mission explainers" in keywords


def test_infer_niche_keywords_adds_diverse_supported_source_phrases_for_generic_finance_seed(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "Global macro investing and stock market analysis",
            "Portfolio strategy and macro trends for long term investors",
            "Stock market outlook, portfolio allocation, investing psychology",
            "Macro research, market cycles, equity strategy",
        ],
    )
    seed = SeedResolution(
        platform=Platform.INSTAGRAM,
        external_id="ig-finance",
        handle="macrovision",
        url="https://www.instagram.com/macrovision/",
        title="Macro Vision",
        description="Global macro investor and stock market strategist",
        uploads_playlist_id=None,
    )

    keywords, _ = niche_service.infer_niche_keywords(seed=seed, competitors=[])

    assert len(keywords) >= 5
    assert any("macro" in keyword for keyword in keywords)
    assert any("stock market" in keyword for keyword in keywords)
    assert sum(
        1
        for keyword in keywords
        if any(marker in keyword for marker in ("portfolio", "equity", "psychology", "strategy", "cycles"))
    ) >= 2


def test_infer_niche_keywords_ignores_offtopic_linked_accounts(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: {
            Platform.YOUTUBE: [
                "Global macro investing for beginners",
                "Stock market outlook and portfolio strategy",
                "Macro analysis of fed and inflation",
            ],
            Platform.INSTAGRAM: [
                "Victoria falls travel guide",
                "Saudi Arabia hotel review",
                "Best waterfalls in africa",
            ],
        }.get(seed.platform, []),
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-finance",
        handle="macrovision",
        url="https://www.youtube.com/@macrovision",
        title="Macro Vision",
        description="Global macro investor and stock market strategist",
        uploads_playlist_id="UU1",
    )
    linked_accounts = [
        SeedResolution(
            platform=Platform.INSTAGRAM,
            external_id="ig-offtopic",
            handle="macrovision",
            url="https://www.instagram.com/macrovision/",
            title="Macro Vision Travel",
            description="Travel, waterfalls and resorts",
            uploads_playlist_id=None,
        )
    ]

    keywords, _ = niche_service.infer_niche_keywords(
        seed=seed,
        competitors=[],
        linked_accounts=linked_accounts,
    )

    assert any("macro" in keyword for keyword in keywords)
    assert any(
        any(marker in keyword for marker in ("stock", "portfolio", "strategy", "inflation"))
        for keyword in keywords
    )
    assert all("victoria" not in keyword and "waterfall" not in keyword and "saudi" not in keyword for keyword in keywords)


def test_infer_niche_keywords_filters_cta_noise_and_keeps_profile_topic_hints(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: [
            "If that sounds interesting, consider subscribing",
            "Comment BOOKS and I'll send you the list",
            "The psychology of making money",
            "My AI strategy for staying relevant",
        ],
    )
    seed = SeedResolution(
        platform=Platform.YOUTUBE,
        external_id="yt-productivity",
        handle="creatorlab",
        url="https://www.youtube.com/@creatorlab",
        title="Creator Lab",
        description="Doctor turned entrepreneur sharing evidence based productivity strategies",
        uploads_playlist_id="UU1",
    )
    linked_accounts = [
        SeedResolution(
            platform=Platform.INSTAGRAM,
            external_id="ig-productivity",
            handle="creatorlab",
            url="https://www.instagram.com/creatorlab/",
            title="Creator Lab",
            description="Evidence based productivity and creator business systems",
            uploads_playlist_id=None,
        )
    ]

    keywords, _ = niche_service.infer_niche_keywords(
        seed=seed,
        competitors=[],
        linked_accounts=linked_accounts,
    )

    assert any("productivity" in keyword or "evidence based" in keyword for keyword in keywords)
    assert all("subscrib" not in keyword and "comment" not in keyword and "send" not in keyword for keyword in keywords)


def test_infer_niche_keywords_filters_affiliate_boilerplate_from_linked_accounts(monkeypatch):
    monkeypatch.setattr(
        niche_service,
        "get_recent_seed_content_texts",
        lambda *, seed, n=10: {
            Platform.INSTAGRAM: [
                "Phone durability test",
                "Tablet teardown",
                "Repairing a game console",
            ],
            Platform.YOUTUBE: [
                "iPhone durability test",
                "Nintendo Switch repair",
                "Laptop teardown",
            ],
        }.get(seed.platform, []),
    )
    seed = SeedResolution(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="randomtechseed",
        url="https://www.instagram.com/randomtechseed/",
        title="Random Tech Seed",
        description="Durability tests and teardowns",
        uploads_playlist_id=None,
    )
    linked_accounts = [
        SeedResolution(
            platform=Platform.YOUTUBE,
            external_id="yt-1",
            handle="randomtechseed",
            url="https://www.youtube.com/@randomtechseed",
            title="Random Tech Seed",
            description=(
                "My main account is @randomtechseed. "
                "EcoFlow Global Ambassador. "
                "SEND ME STUFF: 125 E Main St. Suite 335. "
                "Anything sent to the above address will not be returned. "
                "Contact: randomtechseed at gmail dot com. "
                "Attempt any repairs at your own risk. "
                "This affiliate advertising program is designed to provide a means for sites to earn advertising fees. "
                "As an Amazon Associate I earn from qualifying purchases. "
                "Tech durability tests and teardown videos."
            ),
            uploads_playlist_id="UU1",
        )
    ]

    keywords, _ = niche_service.infer_niche_keywords(
        seed=seed,
        competitors=[],
        linked_accounts=linked_accounts,
    )

    assert any("durability" in keyword or "teardown" in keyword or "repair" in keyword for keyword in keywords)
    assert all(
        bad not in keyword
        for keyword in keywords
        for bad in ("affiliate", "amazon", "advertising", "ambassador", "gmail", "address", "main account")
    )


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
