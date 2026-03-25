from __future__ import annotations

from dataclasses import dataclass

from tracking.models import TgUser, UserCompetitor
from tracking.services.competitor_service import upsert_competitor
from tracking.services.seed_resolver import SeedResolveError, resolve_seed_for_platform


class LivePlatformReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedPlatformReport:
    user: TgUser
    resolved_rows: list[dict[str, str]]
    required_platforms: set[str]


def prepare_live_platform_user(
    *,
    tg_user_id: int,
    tg_chat_id: int,
    timezone_str: str,
    entries: list[tuple[str, str]],
) -> PreparedPlatformReport:
    if not entries:
        raise LivePlatformReportError("Provide at least one platform input")

    user, _ = TgUser.objects.update_or_create(
        tg_user_id=int(tg_user_id),
        defaults={"tg_chat_id": int(tg_chat_id), "timezone_str": str(timezone_str or "UTC")},
    )
    UserCompetitor.objects.filter(user=user).update(is_active=False)

    resolved_rows: list[dict[str, str]] = []
    required_platforms: set[str] = set()
    for platform, raw in entries:
        try:
            seed = resolve_seed_for_platform(platform=platform, raw_input=raw)
        except SeedResolveError as exc:
            raise LivePlatformReportError(f"{platform} resolve failed for {raw}: {exc}") from exc
        except Exception as exc:
            raise LivePlatformReportError(f"{platform} resolve failed for {raw}: {exc}") from exc
        if seed is None:
            raise LivePlatformReportError(f"{platform} resolve failed for {raw}: provider did not verify the profile")
        upsert_competitor(
            user=user,
            platform=seed.platform,
            external_id=seed.external_id,
            handle=seed.handle,
            url=seed.url,
            display_name=seed.title,
            added_by="manual",
            meta={"uploads_playlist_id": seed.uploads_playlist_id} if seed.uploads_playlist_id else None,
        )
        required_platforms.add(seed.platform)
        resolved_rows.append(
            {
                "platform": seed.platform,
                "input": raw,
                "external_id": seed.external_id,
                "handle": seed.handle or "",
                "url": seed.url,
            }
        )

    return PreparedPlatformReport(user=user, resolved_rows=resolved_rows, required_platforms=required_platforms)
