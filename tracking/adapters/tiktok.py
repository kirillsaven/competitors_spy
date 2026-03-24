from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .base import SeedResolution, VideoDetails


class TikTokApiError(RuntimeError):
    pass


_PLAIN_HANDLE_RE = re.compile(r"^[0-9A-Za-z._-]{2,64}$")


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, str) and value:
        raw = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    if value is None:
        raise ValueError("empty datetime")
    return datetime.fromtimestamp(int(value), tz=UTC)


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def extract_handle(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("@") and _PLAIN_HANDLE_RE.match(s[1:]):
        return s[1:]
    if _PLAIN_HANDLE_RE.match(s):
        return s
    try:
        parsed = urlparse(s)
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    if "tiktok." not in host:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    if not parts:
        return None
    first = parts[0]
    if first.startswith("@") and len(first) > 1:
        return first[1:]
    return None


def build_profile_url(handle: str) -> str:
    return f"https://www.tiktok.com/@{handle}"


class ApifyTikTokClient:
    def __init__(
        self,
        *,
        access_token: str,
        actor_id: str,
        base_url: str = "https://api.apify.com/v2",
        timeout_s: float = 30.0,
    ) -> None:
        self.access_token = access_token
        self.actor_id = actor_id
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout_s, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def fetch_profile_feed(self, *, handle: str, results_per_page: int) -> list[dict[str, Any]]:
        if not handle:
            return []
        url = f"{self.base_url}/acts/{quote(self.actor_id, safe='')}/run-sync-get-dataset-items"
        payload = {
            "profiles": [handle],
            "resultsPerPage": max(1, int(results_per_page)),
            "shouldDownloadCovers": False,
            "shouldDownloadSlideshowImages": False,
            "shouldDownloadSubtitles": False,
            "shouldDownloadVideos": False,
        }
        response = self._client.post(
            url,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            },
            json=payload,
        )
        try:
            data = response.json()
        except Exception as exc:
            raise TikTokApiError(f"Apify TikTok API invalid JSON: status={response.status_code}") from exc
        if response.status_code >= 400:
            raise TikTokApiError(f"Apify TikTok API error: status={response.status_code} body={data}")
        if not isinstance(data, list):
            raise TikTokApiError(f"Apify TikTok API returned unexpected payload: {data!r}")
        return [item for item in data if isinstance(item, dict)]


def resolve_seed_input(client: ApifyTikTokClient, raw_input: str) -> SeedResolution | None:
    handle = extract_handle(raw_input)
    if not handle:
        return None

    items = client.fetch_profile_feed(handle=handle, results_per_page=1)
    if not items:
        return SeedResolution(
            platform="tiktok",
            external_id=handle,
            handle=handle,
            url=build_profile_url(handle),
            title=handle,
            description=None,
            uploads_playlist_id=None,
        )

    author_meta = items[0].get("authorMeta") or {}
    author_handle = str(author_meta.get("name") or handle)
    author_id = str(author_meta.get("id") or author_handle)
    return SeedResolution(
        platform="tiktok",
        external_id=author_id,
        handle=author_handle,
        url=build_profile_url(author_handle),
        title=author_meta.get("nickName") or author_handle,
        description=author_meta.get("signature"),
        uploads_playlist_id=None,
    )


def item_to_video_details(item: dict[str, Any]) -> VideoDetails:
    video_id = str(item.get("id") or "")
    if not video_id:
        raise ValueError("TikTok item is missing id")

    author_meta = item.get("authorMeta") or {}
    author_handle = str(author_meta.get("name") or "").strip()
    caption = str(item.get("text") or item.get("desc") or "").strip()
    web_video_url = str(item.get("webVideoUrl") or "").strip()
    if not web_video_url and author_handle:
        web_video_url = f"{build_profile_url(author_handle)}/video/{video_id}"

    video_meta = item.get("videoMeta") or {}
    published_at = _parse_datetime(item.get("createTimeISO") or item.get("createTime"))

    return VideoDetails(
        video_id=video_id,
        url=web_video_url,
        title=caption or f"TikTok {video_id}",
        description=caption,
        published_at=published_at,
        duration_seconds=_to_int(video_meta.get("duration")),
        views=_to_int(item.get("playCount")) or 0,
        likes=_to_int(item.get("diggCount")),
        comments=_to_int(item.get("commentCount")),
        shares=_to_int(item.get("shareCount")),
    )
