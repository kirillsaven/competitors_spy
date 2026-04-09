from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Callable

from tracking.models import TgUser
from tracking.services.competitor_suggest_cache import load_competitor_suggest_cache
from tracking.services.picker_snapshot_cache import (
    load_competitor_remove_picker_snapshot,
    load_stopword_add_picker_snapshot,
    load_stopword_remove_picker_snapshot,
)
from tracking.services.stopword_suggestions import build_user_stopword_suggestions


@dataclass(frozen=True)
class PickerCacheRefreshResult:
    mutation_cache_refresh: bool
    targets: list[str]
    refreshed_targets: list[str]
    refresh_ms: float
    status: str
    failure_reason: str | None = None


def _run_refresh_steps(
    *,
    targets: list[str],
    refreshers: list[tuple[str, Callable[[], object]]],
) -> PickerCacheRefreshResult:
    started_at = perf_counter()
    refreshed_targets: list[str] = []
    failures: list[str] = []
    for target, refresher in refreshers:
        try:
            refresher()
            refreshed_targets.append(target)
        except Exception as exc:
            failures.append(f"{target}: {exc}")
    refresh_ms = (perf_counter() - started_at) * 1000
    if not failures:
        status = "success"
        failure_reason = None
    elif refreshed_targets:
        status = "partial_failure"
        failure_reason = "; ".join(failures)
    else:
        status = "failed"
        failure_reason = "; ".join(failures)
    return PickerCacheRefreshResult(
        mutation_cache_refresh=bool(refreshed_targets),
        targets=list(targets),
        refreshed_targets=refreshed_targets,
        refresh_ms=refresh_ms,
        status=status,
        failure_reason=failure_reason,
    )


def refresh_picker_caches_after_competitor_mutation(
    *,
    user: TgUser,
    suggest_builder: Callable[..., tuple[list[dict], list[str]]],
    stopword_builder: Callable[..., list[str]] = build_user_stopword_suggestions,
) -> PickerCacheRefreshResult:
    targets = ["competitors_suggest", "competitors_remove", "stopwords_add"]
    return _run_refresh_steps(
        targets=targets,
        refreshers=[
            (
                "competitors_suggest",
                lambda: load_competitor_suggest_cache(user=user, builder=suggest_builder),
            ),
            (
                "competitors_remove",
                lambda: load_competitor_remove_picker_snapshot(user=user),
            ),
            (
                "stopwords_add",
                lambda: load_stopword_add_picker_snapshot(user=user, builder=stopword_builder),
            ),
        ],
    )


def refresh_picker_caches_after_stopword_mutation(
    *,
    user: TgUser,
    stopword_builder: Callable[..., list[str]] = build_user_stopword_suggestions,
) -> PickerCacheRefreshResult:
    targets = ["stopwords_add", "stopwords_remove"]
    return _run_refresh_steps(
        targets=targets,
        refreshers=[
            (
                "stopwords_add",
                lambda: load_stopword_add_picker_snapshot(user=user, builder=stopword_builder),
            ),
            (
                "stopwords_remove",
                lambda: load_stopword_remove_picker_snapshot(user=user),
            ),
        ],
    )
