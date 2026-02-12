from __future__ import annotations

from tracking.models import Competitor, TgUser, UserCompetitor


def upsert_competitor(
    *,
    user: TgUser,
    platform: str,
    external_id: str,
    handle: str | None,
    url: str,
    display_name: str | None,
    added_by: str,
    meta: dict | None = None,
) -> Competitor:
    obj, created = Competitor.objects.get_or_create(
        platform=platform,
        external_id=external_id,
        defaults={
            "handle": handle or "",
            "url": url or "",
            "display_name": display_name or "",
            "meta": meta or {},
        },
    )
    changed = False
    for field, value in [
        ("handle", handle or ""),
        ("url", url or ""),
        ("display_name", display_name or ""),
    ]:
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changed = True
    if meta:
        merged = dict(obj.meta or {})
        merged.update(meta)
        if merged != obj.meta:
            obj.meta = merged
            changed = True
    if changed and not created:
        obj.save()

    link, link_created = UserCompetitor.objects.get_or_create(
        user=user,
        competitor=obj,
        defaults={
            "added_by": added_by,
            "is_active": True,
        },
    )
    link_changed = False
    for field, value in [
        ("added_by", added_by),
        ("is_active", True),
    ]:
        if getattr(link, field) != value:
            setattr(link, field, value)
            link_changed = True
    if link_changed and not link_created:
        link.save()
    return obj
