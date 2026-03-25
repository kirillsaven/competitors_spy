from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .base import SeedResolution, VideoDetails


class InstagramApiError(RuntimeError):
    pass


_PLAIN_HANDLE_RE = re.compile(r"^[0-9A-Za-z._]{1,30}$")


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
    if "instagram." not in host:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    if not parts:
        return None
    first = parts[0]
    if first in {"p", "reel", "reels", "stories", "tv"}:
        return None
    return first.lstrip("@") if _PLAIN_HANDLE_RE.match(first.lstrip("@")) else None


def build_profile_url(handle: str) -> str:
    return f"https://www.instagram.com/{handle}/"


class ApifyInstagramClient:
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

    def fetch_profiles(self, *, inputs: list[str]) -> list[dict[str, Any]]:
        sanitized_inputs = [value.strip() for value in inputs if value and value.strip()]
        if not sanitized_inputs:
            return []
        url = f"{self.base_url}/acts/{quote(self.actor_id, safe='')}/run-sync-get-dataset-items"
        payload: dict[str, list[str]] = {}
        direct_urls = [value for value in sanitized_inputs if value.lower().startswith(("http://", "https://"))]
        usernames: list[str] = []
        for value in sanitized_inputs:
            if value.lower().startswith(("http://", "https://")):
                handle = extract_handle(value)
                if handle:
                    usernames.append(handle)
                continue
            if not value.isdigit():
                usernames.append(value)
        user_ids = [value for value in sanitized_inputs if value.isdigit()]
        if direct_urls:
            payload["directUrls"] = direct_urls
        if usernames:
            payload["usernames"] = usernames
        if user_ids:
            payload["userIds"] = user_ids
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
            raise InstagramApiError(f"Apify Instagram API invalid JSON: status={response.status_code}") from exc
        if response.status_code >= 400:
            raise InstagramApiError(f"Apify Instagram API error: status={response.status_code} body={data}")
        if not isinstance(data, list):
            raise InstagramApiError(f"Apify Instagram API returned unexpected payload: {data!r}")
        return [item for item in data if isinstance(item, dict)]


def resolve_seed_input(client: ApifyInstagramClient, raw_input: str) -> SeedResolution | None:
    lookup = (raw_input or "").strip()
    handle = extract_handle(raw_input)
    if not lookup or (not handle and not lookup.lower().startswith(("http://", "https://"))):
        return None

    profiles = client.fetch_profiles(inputs=[lookup])
    if not profiles:
        return None

    profile = profiles[0]
    username = str(profile.get("username") or "").strip()
    profile_id = str(profile.get("id") or "").strip()
    if not username or not profile_id:
        return None

    return SeedResolution(
        platform="instagram",
        external_id=profile_id,
        handle=username,
        url=str(profile.get("url") or build_profile_url(username)),
        title=profile.get("fullName") or username,
        description=profile.get("biography"),
        uploads_playlist_id=None,
    )


def profile_to_video_details(profile: dict[str, Any]) -> list[VideoDetails]:
    posts: list[dict[str, Any]] = []
    for key in ("latestPosts", "latestIgtvVideos"):
        value = profile.get(key)
        if isinstance(value, list):
            posts.extend(item for item in value if isinstance(item, dict))

    out: list[VideoDetails] = []
    seen_ids: set[str] = set()
    for item in posts:
        views = _to_int(item.get("videoViewCount"))
        if views is None:
            continue
        external_id = str(item.get("id") or item.get("shortCode") or item.get("url") or "").strip()
        if not external_id or external_id in seen_ids:
            continue
        seen_ids.add(external_id)

        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or item.get("description") or item.get("caption") or "").strip()
        description = str(item.get("caption") or item.get("description") or title).strip()
        content_type = str(item.get("productType") or item.get("mediaType") or "").strip().lower()
        if not content_type:
            content_type = "video" if views is not None else "image"

        out.append(
            VideoDetails(
                video_id=external_id,
                url=url,
                title=title or f"Instagram {external_id}",
                description=description,
                published_at=_parse_datetime(item.get("timestamp")),
                duration_seconds=_to_int(item.get("videoDuration")),
                views=views,
                likes=_to_int(item.get("likesCount") or item.get("likes")),
                comments=_to_int(item.get("commentsCount") or item.get("comments")),
                shares=_to_int(item.get("sharesCount")),
            )
        )

    return out
