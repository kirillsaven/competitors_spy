from __future__ import annotations

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tracking.models import Platform
from tracking.services.live_platform_report import LivePlatformReportError, prepare_live_platform_user
from tracking.services.report_pipeline import ReportPipelineError, create_and_send_report


class Command(BaseCommand):
    help = "Resolve live platform inputs, build the real report text, print it, and send it to Telegram."

    def add_arguments(self, parser):
        parser.add_argument("--tiktok", action="append", default=[], help="TikTok profile URL or handle. Repeatable.")
        parser.add_argument(
            "--instagram",
            action="append",
            default=[],
            help="Instagram profile URL or handle. Repeatable.",
        )
        parser.add_argument("--timezone", default="UTC", help="Timezone label used when rendering the report text.")
        parser.add_argument("--tg-user-id", type=int, required=True, help="Telegram user id to persist for the test run.")
        parser.add_argument(
            "--tg-chat-id",
            type=int,
            default=None,
            help="Telegram chat id to send the test report to. Defaults to the tg user id.",
        )

    def handle(self, *args, **options):
        entries: list[tuple[str, str]] = []
        entries.extend((Platform.TIKTOK, raw) for raw in (options["tiktok"] or []))
        entries.extend((Platform.INSTAGRAM, raw) for raw in (options["instagram"] or []))
        if not entries:
            raise CommandError("Provide at least one --tiktok or --instagram input")

        tg_user_id = int(options["tg_user_id"])
        tg_chat_id = int(options["tg_chat_id"]) if options["tg_chat_id"] is not None else tg_user_id

        try:
            prepared = prepare_live_platform_user(
                tg_user_id=tg_user_id,
                tg_chat_id=tg_chat_id,
                timezone_str=str(options["timezone"] or "UTC"),
                entries=entries,
            )
        except LivePlatformReportError as exc:
            raise CommandError(str(exc)) from exc

        period_end = timezone.now()
        period_start = period_end - timedelta(hours=24)
        try:
            sent = create_and_send_report(
                user=prepared.user,
                period_start=period_start,
                period_end=period_end,
                required_platforms=prepared.required_platforms,
                provider_fetch_cache=prepared.provider_fetch_cache,
            )
        except ReportPipelineError as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(sent.preview.text.rstrip("\n"))
        self.stdout.write("")
        self.stdout.write(
            json.dumps(
                {
                    "resolved": prepared.resolved_rows,
                    "section_counts": sent.preview.section_counts,
                    "report_id": sent.report.id,
                    "telegram_result": sent.telegram_result,
                    "message_id": sent.telegram_result.get("message_id"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
