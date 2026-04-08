from __future__ import annotations

import logging
from types import SimpleNamespace

from asgiref.sync import async_to_sync

from botapp.picker_open import PreparedPickerOpen, open_picker_with_cache


class DummyMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.reply_markups = []
        self.edit_text_calls = 0

    async def answer(self, text: str, reply_markup=None):
        self.answers.append(text)
        self.reply_markups.append(reply_markup)
        return self

    async def edit_text(self, text: str, reply_markup=None):
        self.edit_text_calls += 1
        self.answers.append(text)
        self.reply_markups.append(reply_markup)
        return self


def test_open_picker_with_cache_uses_single_send_on_hit(caplog):
    logger = logging.getLogger("botapp.test_picker_open")
    message = DummyMessage()
    clear_calls = {"count": 0}
    load_result = SimpleNamespace(cache_hit=True, cache_source="snapshot_hit")

    async def peek():
        return load_result

    async def load():
        raise AssertionError("cold load should not run")

    async def prepare_picker(result):
        assert result is load_result
        return PreparedPickerOpen(text="final", reply_markup=None, row_count=3)

    async def clear_state():
        clear_calls["count"] += 1

    with caplog.at_level(logging.INFO):
        outcome = async_to_sync(open_picker_with_cache)(
            message=message,
            logger=logger,
            event_name="picker_open_observability",
            user_id=1,
            peek=peek,
            load=load,
            loading_text="loading",
            build_failure_text=lambda exc: f"failure: {exc}",
            build_empty_text=lambda result: "empty",
            prepare_picker=prepare_picker,
            clear_state=clear_state,
            build_log_fields=lambda result, row_count: {
                "cache_hit": getattr(result, "cache_hit", False),
                "cache_source": getattr(result, "cache_source", "cold_build"),
                "row_count": row_count,
            },
        )

    assert outcome.open_path == "single_send"
    assert outcome.status == "success"
    assert message.answers == ["final"]
    assert message.edit_text_calls == 0
    assert clear_calls["count"] == 0
    assert "open_path=single_send" in caplog.text
    assert "status=success" in caplog.text


def test_open_picker_with_cache_uses_placeholder_edit_on_miss(caplog):
    logger = logging.getLogger("botapp.test_picker_open")
    message = DummyMessage()
    clear_calls = {"count": 0}
    load_result = SimpleNamespace(cache_hit=False, cache_source="cold_build")

    async def peek():
        return None

    async def load():
        return load_result

    async def prepare_picker(result):
        assert result is load_result
        return PreparedPickerOpen(text="final", reply_markup=None, row_count=2)

    async def clear_state():
        clear_calls["count"] += 1

    with caplog.at_level(logging.INFO):
        outcome = async_to_sync(open_picker_with_cache)(
            message=message,
            logger=logger,
            event_name="picker_open_observability",
            user_id=2,
            peek=peek,
            load=load,
            loading_text="loading",
            build_failure_text=lambda exc: f"failure: {exc}",
            build_empty_text=lambda result: "empty",
            prepare_picker=prepare_picker,
            clear_state=clear_state,
            build_log_fields=lambda result, row_count: {
                "cache_hit": getattr(result, "cache_hit", False),
                "cache_source": getattr(result, "cache_source", "cold_build"),
                "row_count": row_count,
            },
        )

    assert outcome.open_path == "placeholder_edit"
    assert outcome.status == "success"
    assert message.answers == ["loading", "final"]
    assert message.edit_text_calls == 1
    assert clear_calls["count"] == 0
    assert "open_path=placeholder_edit" in caplog.text
    assert "status=success" in caplog.text
