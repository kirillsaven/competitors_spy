from __future__ import annotations

from asgiref.sync import async_to_sync

from botapp.picker_session import (
    PickerSessionKeys,
    picker_page,
    picker_select_all,
    picker_selected_ids,
    picker_set_page,
    picker_toggle_selection,
)


class DummyState:
    def __init__(self) -> None:
        self.data: dict = {}

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)


def test_picker_set_page_updates_and_clamps_state():
    keys = PickerSessionKeys(page_key="page", selected_key="selected")
    state = DummyState()
    data = {"page": 0, "selected": []}

    page_before, page_after = async_to_sync(picker_set_page)(
        state,
        data,
        keys=keys,
        requested_page=9,
        total=12,
        page_size=5,
    )

    assert page_before == 0
    assert page_after == 2
    assert data["page"] == 2
    assert state.data["page"] == 2


def test_picker_toggle_selection_updates_sorted_ids():
    keys = PickerSessionKeys(page_key="page", selected_key="selected")
    state = DummyState()
    data = {"page": 1, "selected": [3, 1]}

    page_before, selected = async_to_sync(picker_toggle_selection)(
        state,
        data,
        keys=keys,
        item_id=2,
    )

    assert page_before == 1
    assert selected == [1, 2, 3]
    assert data["selected"] == [1, 2, 3]
    assert state.data["selected"] == [1, 2, 3]

    _, selected = async_to_sync(picker_toggle_selection)(
        state,
        data,
        keys=keys,
        item_id=1,
    )

    assert selected == [2, 3]
    assert picker_selected_ids(data, keys=keys) == [2, 3]


def test_picker_select_all_normalizes_and_preserves_page():
    keys = PickerSessionKeys(page_key="page", selected_key="selected")
    state = DummyState()
    data = {"page": 2, "selected": []}

    page_before, selected = async_to_sync(picker_select_all)(
        state,
        data,
        keys=keys,
        selected_ids=[4, 2, 2, 0],
    )

    assert page_before == 2
    assert selected == [0, 2, 4]
    assert picker_page(data, keys=keys) == 2
    assert state.data["selected"] == [0, 2, 4]
