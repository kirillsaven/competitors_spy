from __future__ import annotations

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tracking.models import Platform
from tracking.services.live_platform_report import LivePlatformReportError, prepare_live_platform_user
from tracking.services.report_pipeline import ReportPipelineError, assert_required_platform_sections, build_report_preview


class Command(BaseCommand):
    help = "Resolve live TikTok/Instagram competitors, collect fresh provider data, and render a local report preview."

    def add_arguments(self, parser):
        parser.add_argument("--tiktok", action="append", default=[], help="TikTok profile URL or handle. Repeatable.")
        parser.add_argument(
            "--instagram",
            action="append",
            default=[],
            help="Instagram profile URL or handle. Repeatable.",
        )
        parser.add_argument("--timezone", default="UTC", help="Timezone label used when rendering the report preview.")
        parser.add_argument(
            "--tg-user-id",
            type=int,
            default=990000001,
            help="Synthetic TgUser id used for local verification state.",
        )
        parser.add_argument(
            "--tg-chat-id",
            type=int,
            default=990000001,
            help="Synthetic chat id used for local verification state.",
        )

    def handle(self, *args, **options):
        entries: list[tuple[str, str]] = []
        entries.extend((Platform.TIKTOK, raw) for raw in (options["tiktok"] or []))
        entries.extend((Platform.INSTAGRAM, raw) for raw in (options["instagram"] or []))
        if not entries:
            raise CommandError("Provide at least one --tiktok or --instagram input")

        for platform, raw in entries:
            if not raw:
                raise CommandError(f"Empty input for platform: {platform}")

        try:
            prepared = prepare_live_platform_user(
                tg_user_id=int(options["tg_user_id"]),
                tg_chat_id=int(options["tg_chat_id"]),
                timezone_str=str(options["timezone"] or "UTC"),
                entries=entries,
            )
        except LivePlatformReportError as exc:
            raise CommandError(str(exc)) from exc

        period_end = timezone.now()
        period_start = period_end - timedelta(hours=24)
        preview = build_report_preview(
            user=prepared.user,
            period_start=period_start,
            period_end=period_end,
            provider_fetch_cache=prepared.provider_fetch_cache,
        )
        try:
            assert_required_platform_sections(preview=preview, required_platforms=prepared.required_platforms)
        except ReportPipelineError as exc:
            raise CommandError(str(exc)) from exc

        output = {
            "resolved": prepared.resolved_rows,
            "section_counts": preview.section_counts,
            "payload": preview.payload,
            "report_text": preview.text,
        }
        self.stdout.write(json.dumps(output, ensure_ascii=False, indent=2))
