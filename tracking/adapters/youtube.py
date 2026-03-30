from __future__ import annotations

from datetime import UTC, datetime
import re
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from common.time import parse_iso8601_duration_seconds

from .base import CompetitorCandidate, SeedResolution, VideoDetails


class YouTubeApiError(RuntimeError):
    pass


_PLAIN_HANDLE_RE = re.compile(r"^[0-9A-Za-z._-]{3,50}$")
_PLAIN_CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
_QUOTA_ERROR_MARKERS = {
    "quotaexceeded",
    "dailylimitexceeded",
    "userratelimitexceeded",
    "ratelimitexceeded",
}
_KEY_POOL_LOCK = Lock()
_KEY_POOL_INDEX: dict[tuple[str, ...], int] = {}


def _parse_rfc3339(value: str) -> datetime:
    if not value:
        raise ValueError("empty datetime")
    v = value
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _format_rfc3339(value: datetime) -> str:
    dt = value.astimezone(UTC).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def _to_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def extract_handle(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("@"):
        return s[1:]
    # Allow plain handle/nickname (no '@') to keep UX simple.
    if _PLAIN_HANDLE_RE.match(s) and not _PLAIN_CHANNEL_ID_RE.match(s):
        return s
    try:
        u = urlparse(s)
    except Exception:
        return None
    if u.netloc and "youtube." in u.netloc:
        path = u.path or ""
        if path.startswith("/@"):
            handle = path.split("/", 2)[1][1:]
            if handle:
                return handle
    return None


def extract_channel_id(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    # Allow pasting channelId directly (e.g. UCxxxxxxxxxxxxxxxxxxxxxx).
    if _PLAIN_CHANNEL_ID_RE.match(s):
        return s
    try:
        u = urlparse(s)
    except Exception:
        return None
    if u.netloc and "youtube." in u.netloc:
        parts = [p for p in (u.path or "").split("/") if p]
        if len(parts) >= 2 and parts[0] == "channel":
            return parts[1]
    return None


def extract_video_id(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        u = urlparse(s)
    except Exception:
        return None

    if not u.netloc:
        return None
    if "youtube." not in u.netloc and "youtu.be" not in u.netloc:
        return None

    if u.netloc == "youtu.be":
        vid = (u.path or "").strip("/").split("/", 1)[0]
        return vid or None

    qs = parse_qs(u.query or "")
    if "v" in qs and qs["v"]:
        return qs["v"][0]

    parts = [p for p in (u.path or "").split("/") if p]
    if len(parts) >= 2 and parts[0] == "shorts":
        return parts[1]
    return None


class YouTubeClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_keys: list[str] | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        keys: list[str] = []
        for raw in list(api_keys or []):
            key = str(raw or "").strip()
            if key and key not in keys:
                keys.append(key)
        single = str(api_key or "").strip()
        if single and single not in keys:
            keys.append(single)
        if not keys:
            raise ValueError("YouTubeClient requires at least one API key")
        self.api_keys = keys
        self.api_key = keys[0]
        self._key_pool = tuple(keys)
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def _pool_index(self) -> int:
        with _KEY_POOL_LOCK:
            return int(_KEY_POOL_INDEX.get(self._key_pool, 0) or 0) % len(self.api_keys)

    def _set_pool_index(self, index: int) -> None:
        with _KEY_POOL_LOCK:
            _KEY_POOL_INDEX[self._key_pool] = int(index) % len(self.api_keys)

    def _request(self, *, api_key: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"https://www.googleapis.com/youtube/v3/{path.lstrip('/')}"
        params = {**params, "key": api_key}
        r = self._client.get(url, params=params)
        try:
            data = r.json()
        except Exception:
            raise YouTubeApiError(f"YouTube API invalid JSON: status={r.status_code}")
        if r.status_code >= 400 or "error" in data:
            raise YouTubeApiError(f"YouTube API error: status={r.status_code} body={data}")
        return data

    def _is_quota_error(self, exc: YouTubeApiError) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in _QUOTA_ERROR_MARKERS)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: YouTubeApiError | None = None
        start_index = self._pool_index()
        for offset in range(len(self.api_keys)):
            index = (start_index + offset) % len(self.api_keys)
            api_key = self.api_keys[index]
            try:
                data = self._request(api_key=api_key, path=path, params=params)
            except YouTubeApiError as exc:
                last_error = exc
                if self._is_quota_error(exc) and offset + 1 < len(self.api_keys):
                    self._set_pool_index(index + 1)
                    continue
                raise
            self.api_key = api_key
            self._set_pool_index(index)
            return data
        if last_error is not None:
            raise last_error
        raise YouTubeApiError("YouTube API request failed without a specific error")

    def channels_list(self, *, part: str, for_handle: str | None = None, ids: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"part": part, "maxResults": 50}
        if for_handle:
            params["forHandle"] = for_handle
        if ids:
            params["id"] = ",".join(ids)
        data = self._get("channels", params)
        return data.get("items", []) or []

    def channel_sections(self, *, channel_id: str) -> list[dict[str, Any]]:
        data = self._get(
            "channelSections",
            {
                "part": "snippet,contentDetails",
                "channelId": channel_id,
                "maxResults": 50,
            },
        )
        return data.get("items", []) or []

    def playlist_items(self, *, playlist_id: str, max_results: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        remaining = max_results
        while remaining > 0:
            batch = min(50, remaining)
            params: dict[str, Any] = {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": batch,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get("playlistItems", params)
            items = data.get("items", []) or []
            out.extend(items)
            remaining -= len(items)
            page_token = data.get("nextPageToken")
            if not page_token or not items:
                break
        return out

    def playlist_items_page(
        self,
        *,
        playlist_id: str,
        max_results: int,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": min(50, max(1, int(max_results))),
        }
        if page_token:
            params["pageToken"] = page_token
        data = self._get("playlistItems", params)
        items = data.get("items", []) or []
        next_page_token = data.get("nextPageToken")
        return items, str(next_page_token) if next_page_token else None

    def videos_list(self, *, ids: list[str], part: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            data = self._get(
                "videos",
                {
                    "part": part,
                    "id": ",".join(batch),
                    "maxResults": 50,
                },
            )
            out.extend(data.get("items", []) or [])
        return out

    def search_channels(self, *, q: str, max_results: int) -> list[str]:
        data = self._get(
            "search",
            {
                "part": "snippet",
                "type": "channel",
                "q": q,
                "maxResults": max_results,
            },
        )
        out: list[str] = []
        for it in data.get("items", []) or []:
            cid = (it.get("id") or {}).get("channelId")
            if cid:
                out.append(cid)
        return out

    def search_videos_page(
        self,
        *,
        q: str,
        max_results: int,
        page_token: str | None = None,
        published_after: datetime | None = None,
        short_duration_only: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "q": q,
            "maxResults": min(50, max(1, int(max_results))),
        }
        if page_token:
            params["pageToken"] = page_token
        if published_after is not None:
            params["publishedAfter"] = _format_rfc3339(published_after)
        if short_duration_only:
            params["videoDuration"] = "short"
        data = self._get("search", params)
        items = data.get("items", []) or []
        next_page_token = data.get("nextPageToken")
        return items, str(next_page_token) if next_page_token else None


def resolve_seed_input(client: YouTubeClient, raw_input: str) -> SeedResolution | None:
    handle = extract_handle(raw_input)
    channel_id = extract_channel_id(raw_input)
    video_id = extract_video_id(raw_input)

    channel_item: dict[str, Any] | None = None
    if handle:
        items = client.channels_list(part="snippet,contentDetails,statistics", for_handle=handle)
        channel_item = items[0] if items else None
    elif channel_id:
        items = client.channels_list(part="snippet,contentDetails,statistics", ids=[channel_id])
        channel_item = items[0] if items else None
    elif video_id:
        vids = client.videos_list(ids=[video_id], part="snippet")
        if vids:
            cid = (vids[0].get("snippet") or {}).get("channelId")
            if cid:
                items = client.channels_list(part="snippet,contentDetails,statistics", ids=[cid])
                channel_item = items[0] if items else None
    else:
        return None

    if not channel_item:
        return None

    cid = channel_item.get("id")
    snippet = channel_item.get("snippet") or {}
    content_details = channel_item.get("contentDetails") or {}
    stats = channel_item.get("statistics") or {}
    uploads = ((content_details.get("relatedPlaylists") or {}).get("uploads")) if content_details else None
    title = snippet.get("title")
    desc = snippet.get("description")
    url = f"https://www.youtube.com/channel/{cid}" if cid else ""
    thumbnails = snippet.get("thumbnails") or {}
    image_url = str(
        (thumbnails.get("high") or {}).get("url")
        or (thumbnails.get("medium") or {}).get("url")
        or (thumbnails.get("default") or {}).get("url")
        or ""
    ).strip() or None
    return SeedResolution(
        platform="youtube",
        external_id=str(cid),
        handle=handle,
        url=url,
        title=title,
        description=desc,
        uploads_playlist_id=uploads,
        subscriber_count=_to_int(stats.get("subscriberCount")),
        image_url=image_url,
    )


def extract_featured_channel_ids(sections: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for it in sections:
        cd = it.get("contentDetails") or {}
        channels = cd.get("channels") or []
        for cid in channels:
            if cid and cid not in out:
                out.append(cid)
    return out


def channel_items_to_candidates(channel_items: list[dict[str, Any]], *, reason: str) -> list[CompetitorCandidate]:
    out: list[CompetitorCandidate] = []
    for it in channel_items:
        cid = it.get("id")
        snippet = it.get("snippet") or {}
        title = snippet.get("title")
        custom_url = snippet.get("customUrl") or ""
        handle = custom_url[1:] if isinstance(custom_url, str) and custom_url.startswith("@") else None
        url = f"https://www.youtube.com/channel/{cid}" if cid else ""
        out.append(
            CompetitorCandidate(
                platform="youtube",
                external_id=str(cid),
                handle=handle,
                url=url,
                display_name=title,
                reason=reason,
            )
        )
    return out


def playlist_items_to_video_ids(items: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for it in items:
        cd = it.get("contentDetails") or {}
        vid = cd.get("videoId")
        if vid:
            out.append(vid)
    return out


def video_items_to_details(video_items: list[dict[str, Any]]) -> list[VideoDetails]:
    out: list[VideoDetails] = []
    for it in video_items:
        vid = it.get("id")
        snippet = it.get("snippet") or {}
        stats = it.get("statistics") or {}
        cdetails = it.get("contentDetails") or {}

        published_at = _parse_rfc3339(snippet.get("publishedAt"))
        duration_seconds = parse_iso8601_duration_seconds(cdetails.get("duration") or "")

        views = int(stats.get("viewCount") or 0)
        likes_raw = stats.get("likeCount")
        comments_raw = stats.get("commentCount")

        out.append(
            VideoDetails(
                video_id=str(vid),
                url=f"https://www.youtube.com/watch?v={vid}",
                title=snippet.get("title") or "",
                description=snippet.get("description") or "",
                published_at=published_at,
                duration_seconds=duration_seconds,
                views=views,
                likes=int(likes_raw) if likes_raw is not None else None,
                comments=int(comments_raw) if comments_raw is not None else None,
            )
        )
    return out
