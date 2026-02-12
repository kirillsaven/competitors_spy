from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tracking.models import TgUser
from tracking.tasks import run_user_report


class Command(BaseCommand):
    help = "Enqueue a report run for a user by tg_user_id. Use CELERY_TASK_ALWAYS_EAGER=1 for sync execution."

    def add_arguments(self, parser):
        parser.add_argument("tg_user_id", type=int)

    def handle(self, *args, **options):
        tg_user_id = options["tg_user_id"]
        user = TgUser.objects.filter(tg_user_id=tg_user_id).first()
        if not user:
            raise CommandError(f"TgUser not found: {tg_user_id}")
        run_user_report.delay(user.id)
        self.stdout.write(self.style.SUCCESS(f"Enqueued run_user_report for tg_user_id={tg_user_id} (user_id={user.id})"))

