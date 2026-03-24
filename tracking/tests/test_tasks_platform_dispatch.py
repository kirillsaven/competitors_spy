from __future__ import annotations

from tracking.models import Competitor, Platform, TgUser, UserCompetitor
from tracking.tasks import _get_active_competitors


def test_get_active_competitors_returns_platform_ordered_list(db):
    user = TgUser.objects.create(tg_user_id=1, tg_chat_id=1)
    youtube = Competitor.objects.create(platform=Platform.YOUTUBE, external_id="yt-1")
    tiktok = Competitor.objects.create(platform=Platform.TIKTOK, external_id="tt-1")
    instagram = Competitor.objects.create(platform=Platform.INSTAGRAM, external_id="ig-1")

    UserCompetitor.objects.create(user=user, competitor=tiktok)
    UserCompetitor.objects.create(user=user, competitor=instagram)
    UserCompetitor.objects.create(user=user, competitor=youtube)

    competitors = _get_active_competitors(user=user)

    assert [competitor.platform for competitor in competitors] == [
        Platform.YOUTUBE,
        Platform.TIKTOK,
        Platform.INSTAGRAM,
    ]
