from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from time import perf_counter
from typing import Callable

from django.conf import settings
from django.db.models import Count, Max

from tracking.models import ContentItem, MetricSnapshot, TgUser, UserCompetitor
from tracking.services.competitor_service import list_active_user_competitor_links
from tracking.services.report_filters import get_user_report_stopwords
from tracking.services.setup_retry_cache import get_cached_retry_value, store_retry_value
from tracking.services.stopword_suggestions import build_user_stopword_suggestions

_PICKER_SNAPSHOT_CACHE_VERSION = "v1"
PICKER_SNAPSHOT_CACHE_SOURCE_COLD_BUILD = "cold_build"
PICKER_SNAPSHOT_CACHE_SOURCE_SNAPSHOT_HIT = "snapshot_hit"


@dataclass(frozen=True)
class PickerSnapshotLoadResult:
    payload: dict
    cache_hit: bool
    cache_source: str
    build_ms: float


def _picker_snapshot_cache_ttl_seconds() -> int:
    return max(1, int(getattr(settings, "PICKER_SNAPSHOT_CACHE_TTL_SECONDS", 900) or 900))


def _timestamp_value(value) -> str:
    if value is None:
        return ""
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def _digest_payload(*, kind: str, payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"picker-snapshot::{kind}::{digest}"


def _load_cached_snapshot(*, cache_key: str, builder: Callable[[], dict]) -> PickerSnapshotLoadResult:
    cached, cache_hit = get_cached_retry_value(cache_key)
    if cache_hit and isinstance(cached, dict):
        return PickerSnapshotLoadResult(
            payload=deepcopy(cached),
            cache_hit=True,
            cache_source=PICKER_SNAPSHOT_CACHE_SOURCE_SNAPSHOT_HIT,
            build_ms=0.0,
        )

    started_at = perf_counter()
    payload = builder()
    build_ms = (perf_counter() - started_at) * 1000
    store_retry_value(cache_key, deepcopy(payload), ttl_seconds=_picker_snapshot_cache_ttl_seconds())
    return PickerSnapshotLoadResult(
        payload=deepcopy(payload),
        cache_hit=False,
        cache_source=PICKER_SNAPSHOT_CACHE_SOURCE_COLD_BUILD,
        build_ms=build_ms,
    )


def _active_competitor_link_snapshot(*, user: TgUser) -> list[dict]:
    rows: list[dict] = []
    for link in list_active_user_competitor_links(user=user):
        competitor = link.competitor
        name = competitor.display_name or competitor.handle or competitor.external_id
        handle = str(competitor.handle or "").strip().lstrip("@")
        display_name = f"{name} (@{handle})" if handle else str(name)
        rows.append(
            {
                "link_id": int(link.id),
                "link_updated_at": _timestamp_value(link.updated_at),
                "competitor_id": int(link.competitor_id),
                "platform": str(competitor.platform or ""),
                "external_id": str(competitor.external_id or "").strip(),
                "handle": str(competitor.handle or "").strip(),
                "display_name": display_name.strip(),
                "url": str(competitor.url or "").strip(),
                "competitor_updated_at": _timestamp_value(competitor.updated_at),
            }
        )
    return rows


def _competitor_remove_picker_payload(*, user: TgUser) -> dict:
    rows = _active_competitor_link_snapshot(user=user)
    return {
        "rows": [
            {
                "competitor_id": int(row["competitor_id"]),
                "platform": str(row["platform"]),
                "display_name": str(row["display_name"]),
                "url": str(row["url"]).strip() or None,
            }
            for row in rows
        ],
        "picker_rows": [
            {
                "id": int(row["competitor_id"]),
                "name": str(row["display_name"]),
                "url": str(row["url"]).strip() or None,
            }
            for row in rows
        ],
        "platform_by_id": {
            str(int(row["competitor_id"])): str(row["platform"])
            for row in rows
            if int(row["competitor_id"]) > 0
        },
    }


def load_competitor_remove_picker_snapshot(*, user: TgUser) -> PickerSnapshotLoadResult:
    snapshot_payload = {
        "version": _PICKER_SNAPSHOT_CACHE_VERSION,
        "user_id": int(user.id),
        "active_links": _active_competitor_link_snapshot(user=user),
    }
    return _load_cached_snapshot(
        cache_key=_digest_payload(kind="competitors_remove", payload=snapshot_payload),
        builder=lambda: _competitor_remove_picker_payload(user=user),
    )


def _active_competitor_freshness_snapshot(*, user: TgUser) -> dict:
    competitor_ids = [int(link.competitor_id) for link in list_active_user_competitor_links(user=user)]
    if not competitor_ids:
        return {
            "competitor_ids": [],
            "content_count": 0,
            "latest_content_created_at": "",
            "latest_content_published_at": "",
            "metric_count": 0,
            "latest_metric_captured_at": "",
        }
    content_fingerprint = ContentItem.objects.filter(competitor_id__in=competitor_ids).aggregate(
        content_count=Count("id"),
        latest_content_created_at=Max("created_at"),
        latest_content_published_at=Max("published_at"),
    )
    metric_fingerprint = MetricSnapshot.objects.filter(content_item__competitor_id__in=competitor_ids).aggregate(
        metric_count=Count("id"),
        latest_metric_captured_at=Max("captured_at"),
    )
    return {
        "competitor_ids": competitor_ids,
        "content_count": int(content_fingerprint.get("content_count") or 0),
        "latest_content_created_at": _timestamp_value(content_fingerprint.get("latest_content_created_at")),
        "latest_content_published_at": _timestamp_value(content_fingerprint.get("latest_content_published_at")),
        "metric_count": int(metric_fingerprint.get("metric_count") or 0),
        "latest_metric_captured_at": _timestamp_value(metric_fingerprint.get("latest_metric_captured_at")),
    }


def load_stopword_add_picker_snapshot(
    *,
    user: TgUser,
    builder: Callable[..., list[str]] = build_user_stopword_suggestions,
) -> PickerSnapshotLoadResult:
    snapshot_payload = {
        "version": _PICKER_SNAPSHOT_CACHE_VERSION,
        "user_id": int(user.id),
        "stopwords": get_user_report_stopwords(user=user),
        "active_competitors": _active_competitor_link_snapshot(user=user),
        "content_freshness": _active_competitor_freshness_snapshot(user=user),
    }
    return _load_cached_snapshot(
        cache_key=_digest_payload(kind="stopwords_add", payload=snapshot_payload),
        builder=lambda: {"items": list(builder(user=user))},
    )


def load_stopword_remove_picker_snapshot(*, user: TgUser) -> PickerSnapshotLoadResult:
    snapshot_payload = {
        "version": _PICKER_SNAPSHOT_CACHE_VERSION,
        "user_id": int(user.id),
        "stopwords": get_user_report_stopwords(user=user),
    }
    return _load_cached_snapshot(
        cache_key=_digest_payload(kind="stopwords_remove", payload=snapshot_payload),
        builder=lambda: {"items": get_user_report_stopwords(user=user)},
    )
