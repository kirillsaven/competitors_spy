from __future__ import annotations

from tracking.adapters.youtube import extract_channel_id, extract_handle, extract_video_id


def test_extract_handle() -> None:
    assert extract_handle("@foo") == "foo"
    assert extract_handle("https://www.youtube.com/@foo") == "foo"


def test_extract_channel_id() -> None:
    assert extract_channel_id("https://www.youtube.com/channel/UC123") == "UC123"


def test_extract_video_id() -> None:
    assert extract_video_id("https://www.youtube.com/watch?v=abc123") == "abc123"
    assert extract_video_id("https://youtu.be/abc123") == "abc123"
    assert extract_video_id("https://www.youtube.com/shorts/abc123") == "abc123"

