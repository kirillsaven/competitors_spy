from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, MutableMapping


@dataclass(frozen=True)
class PickerSessionKeys:
    page_key: str
    selected_key: str


def picker_page(data: Mapping[str, Any], *, keys: PickerSessionKeys) -> int:
    return int(data.get(keys.page_key) or 0)


def picker_selected_ids(data: Mapping[str, Any], *, keys: PickerSessionKeys) -> list[int]:
    selected: set[int] = set()
    for item in list(data.get(keys.selected_key) or []):
        try:
            selected.add(int(item))
        except (TypeError, ValueError):
            continue
    return sorted(selected)


def picker_selected_set(data: Mapping[str, Any], *, keys: PickerSessionKeys) -> set[int]:
    return set(picker_selected_ids(data, keys=keys))


def _picker_page_count(*, total: int, page_size: int) -> int:
    return max(1, (max(0, total) + page_size - 1) // page_size)


def clamp_picker_page(requested_page: int, *, total: int, page_size: int) -> int:
    return min(max(0, int(requested_page)), _picker_page_count(total=total, page_size=page_size) - 1)


async def picker_set_page(
    state,
    data: MutableMapping[str, Any],
    *,
    keys: PickerSessionKeys,
    requested_page: int,
    total: int,
    page_size: int,
) -> tuple[int, int]:
    page_before = picker_page(data, keys=keys)
    page_after = clamp_picker_page(requested_page, total=total, page_size=page_size)
    await state.update_data(**{keys.page_key: page_after})
    data[keys.page_key] = page_after
    return page_before, page_after


async def picker_toggle_selection(
    state,
    data: MutableMapping[str, Any],
    *,
    keys: PickerSessionKeys,
    item_id: int,
) -> tuple[int, list[int]]:
    page_before = picker_page(data, keys=keys)
    selected = picker_selected_set(data, keys=keys)
    if item_id in selected:
        selected.remove(item_id)
    else:
        selected.add(item_id)
    selected_sorted = sorted(selected)
    await state.update_data(**{keys.selected_key: selected_sorted})
    data[keys.selected_key] = selected_sorted
    return page_before, selected_sorted


async def picker_select_all(
    state,
    data: MutableMapping[str, Any],
    *,
    keys: PickerSessionKeys,
    selected_ids: Iterable[int],
) -> tuple[int, list[int]]:
    page_before = picker_page(data, keys=keys)
    normalized = sorted({int(item) for item in selected_ids})
    await state.update_data(**{keys.selected_key: normalized})
    data[keys.selected_key] = normalized
    return page_before, normalized
