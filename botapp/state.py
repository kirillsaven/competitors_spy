from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SetupStates(StatesGroup):
    WAIT_SEED_INPUT = State()
    WAIT_COMPETITOR_LIST = State()
    CONFIRM_OR_EDIT_NICHE = State()
    WAIT_MANUAL_NICHE = State()
    PRUNE_COMPETITORS = State()
    ASK_TIMEZONE_METHOD = State()
    WAIT_LOCATION = State()
    WAIT_TZ_MANUAL = State()
    ASK_REPORTS_PER_DAY = State()
    PICK_TIME_SINGLE = State()
    PICK_TIME_PAIR = State()
    PICK_TIME_CUSTOM_1 = State()
    WAIT_TIME_1 = State()
    PICK_TIME_CUSTOM_2 = State()
    WAIT_TIME_2 = State()
