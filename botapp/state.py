from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SetupStates(StatesGroup):
    WAIT_SEED_INPUT = State()
    CONFIRM_OR_EDIT_NICHE = State()
    WAIT_MANUAL_NICHE = State()
    WAIT_COMPETITOR_LIST = State()
    SHOW_AUTO_CANDIDATES = State()
    REVIEW_FINAL_COMPETITORS = State()
    ASK_REPORTS_PER_DAY = State()
    WAIT_TIME_1 = State()
    WAIT_TIME_2 = State()
    ASK_TIMEZONE_METHOD = State()
    WAIT_LOCATION = State()
    WAIT_TZ_MANUAL = State()

