from django.test import TestCase

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
