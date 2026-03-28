from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from tracking.models import Platform


PLATFORM_STATE_AVAILABLE = "AVAILABLE"
PLATFORM_STATE_UNAVAILABLE = "UNAVAILABLE"
PLATFORM_STATE_ERROR = "ERROR"
PLATFORM_STATE_SKIPPED = "SKIPPED"

_SUPPORTED_PLATFORMS = (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)
_HARD_UNAVAILABLE_MARKERS = (
    "status=403",
    "platform-feature-disabled",
    "hard limit",
    "access token is not set",
    "provider_access_token is not set",
    "provider_base_url is not set",
    "profile_actor_id is not set",
    "search_actor_id is not set",
    "unsupported ",
)


@dataclass
class PlatformRuntimeState:
    state: str = PLATFORM_STATE_AVAILABLE
    reason: str = ""


@dataclass
class SetupRunContext:
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    seed_resolution_cache: dict[str, tuple[object | None, str | None]] = field(default_factory=dict)
    profile_cache: dict[str, object] = field(default_factory=dict)
    recent_content_cache: dict[str, list[str]] = field(default_factory=dict)
    collectible_signals_cache: dict[str, tuple[list[str], list[int], int]] = field(default_factory=dict)
    search_cache: dict[str, object] = field(default_factory=dict)
    discovery_diagnostics: dict[str, object] = field(default_factory=dict)
    platform_states: dict[str, PlatformRuntimeState] = field(
        default_factory=lambda: {
            platform: PlatformRuntimeState()
            for platform in _SUPPORTED_PLATFORMS
        }
    )


def get_platform_state(context: SetupRunContext | None, platform: str) -> PlatformRuntimeState:
    if context is None:
        return PlatformRuntimeState()
    platform_key = str(platform or "")
    if platform_key not in context.platform_states:
        context.platform_states[platform_key] = PlatformRuntimeState()
    return context.platform_states[platform_key]


def set_platform_state(context: SetupRunContext | None, *, platform: str, state: str, reason: str = "") -> None:
    if context is None:
        return
    runtime_state = get_platform_state(context, platform)
    runtime_state.state = state
    runtime_state.reason = str(reason or "").strip()


def mark_platform_available(context: SetupRunContext | None, platform: str) -> None:
    if context is None:
        return
    runtime_state = get_platform_state(context, platform)
    if runtime_state.state == PLATFORM_STATE_AVAILABLE and not runtime_state.reason:
        return
    runtime_state.state = PLATFORM_STATE_AVAILABLE
    runtime_state.reason = ""


def mark_platform_skipped(context: SetupRunContext | None, platform: str, *, reason: str) -> None:
    set_platform_state(context, platform=platform, state=PLATFORM_STATE_SKIPPED, reason=reason)


def is_hard_unavailable_error(message: str | None) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in _HARD_UNAVAILABLE_MARKERS)


def mark_platform_failure(context: SetupRunContext | None, *, platform: str, reason: str) -> str:
    state = PLATFORM_STATE_UNAVAILABLE if is_hard_unavailable_error(reason) else PLATFORM_STATE_ERROR
    set_platform_state(context, platform=platform, state=state, reason=reason)
    return state


def platform_is_blocked(context: SetupRunContext | None, platform: str) -> bool:
    state = get_platform_state(context, platform)
    return state.state in {PLATFORM_STATE_UNAVAILABLE, PLATFORM_STATE_SKIPPED}
