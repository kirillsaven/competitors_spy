from django.test import TestCase

from botapp.keyboards import kb_prune_competitors, kb_seed_candidates
from botapp.user_sync import normalize_profile_text, normalize_tg_username, upsert_tg_user
from tracking.models import TgUser


class UserSyncTests(TestCase):
    def test_normalize_tg_username_strips_at_prefix(self) -> None:
        self.assertEqual(normalize_tg_username("@spy_user"), "spy_user")
        self.assertEqual(normalize_tg_username("spy_user"), "spy_user")
        self.assertEqual(normalize_tg_username(None), "")

    def test_normalize_profile_text_handles_none(self) -> None:
        self.assertEqual(normalize_profile_text("  Alice  "), "Alice")
        self.assertEqual(normalize_profile_text(None), "")

    def test_upsert_tg_user_saves_username_and_chat(self) -> None:
        user, created = upsert_tg_user(
            telegram_user_id=777,
            chat_id=888,
            username="@spy_user",
            first_name="Alice",
            last_name="Smith",
            language_code="ru",
        )

        self.assertTrue(created)
        self.assertEqual(user.tg_user_id, 777)
        self.assertEqual(user.tg_chat_id, 888)
        self.assertEqual(user.tg_username, "spy_user")
        self.assertEqual(user.tg_first_name, "Alice")
        self.assertEqual(user.tg_last_name, "Smith")
        self.assertEqual(user.tg_language_code, "ru")

    def test_upsert_tg_user_updates_existing_username(self) -> None:
        TgUser.objects.create(
            tg_user_id=777,
            tg_chat_id=888,
            tg_username="old_name",
            tg_first_name="Old",
            tg_last_name="Name",
            tg_language_code="en",
        )

        user, created = upsert_tg_user(
            telegram_user_id=777,
            chat_id=999,
            username="new_name",
            first_name="New",
            last_name="Person",
            language_code="ru",
        )

        self.assertFalse(created)
        self.assertEqual(user.tg_chat_id, 999)
        self.assertEqual(user.tg_username, "new_name")
        self.assertEqual(user.tg_first_name, "New")
        self.assertEqual(user.tg_last_name, "Person")
        self.assertEqual(user.tg_language_code, "ru")


class CompetitorPickerKeyboardTests(TestCase):
    def test_prune_keyboard_adds_profile_link_button_for_valid_url(self) -> None:
        markup = kb_prune_competitors(
            competitor_rows=[(0, "[YouTube] Daria Pancho", "https://www.youtube.com/@dariapancho")],
            excluded_ids=set(),
            page=0,
            page_size=8,
        )

        first_row = markup.inline_keyboard[0]
        self.assertEqual(len(first_row), 1)
        self.assertEqual(first_row[0].callback_data, "prune_toggle:0")
        self.assertEqual(first_row[0].text, "✅ 1. [YouTube] Daria Pancho")

        second_row = markup.inline_keyboard[1]
        self.assertEqual(len(second_row), 1)
        self.assertEqual(second_row[0].text, "1↗")
        self.assertEqual(second_row[0].url, "https://www.youtube.com/@dariapancho")

    def test_prune_keyboard_skips_profile_link_button_for_invalid_url(self) -> None:
        markup = kb_prune_competitors(
            competitor_rows=[(0, "[YouTube] Daria Pancho", "youtube.com/@dariapancho")],
            excluded_ids=set(),
            page=0,
            page_size=8,
        )

        first_row = markup.inline_keyboard[0]
        self.assertEqual(len(first_row), 1)
        self.assertEqual(first_row[0].callback_data, "prune_toggle:0")
        self.assertEqual(first_row[0].text, "✅ 1. [YouTube] Daria Pancho")


class CompetitorDisplayNameTests(TestCase):
    def test_candidate_display_name_uses_short_platform_labels(self) -> None:
        from botapp.handlers.setup import _candidate_display_name

        self.assertEqual(
            _candidate_display_name({"platform": "youtube", "display_name": "Daria Pancho"}),
            "[YT] Daria Pancho",
        )
        self.assertEqual(
            _candidate_display_name({"platform": "tiktok", "display_name": "Speak Easy"}),
            "[TT] Speak Easy",
        )


class SeedPickerKeyboardTests(TestCase):
    def test_seed_keyboard_shows_platform_chip_and_profile_link(self) -> None:
        markup = kb_seed_candidates(
            candidates=[
                {
                    "platform": "instagram",
                    "title": "Anatoliy",
                    "handle": "anatoliypanov",
                    "url": "https://www.instagram.com/anatoliypanov/",
                }
            ]
        )

        first_row = markup.inline_keyboard[0]
        self.assertEqual(first_row[0].text, "[IG] Anatoliy (@anatoliypanov)")
        self.assertEqual(first_row[0].callback_data, "seed_pick:0")
        self.assertEqual(first_row[1].text, "↗")
        self.assertEqual(first_row[1].url, "https://www.instagram.com/anatoliypanov/")
