from __future__ import annotations

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tracking.models import Platform, TgUser, UserCompetitor
from tracking.services.collector import refresh_competitor
from tracking.services.competitor_service import upsert_competitor
from tracking.services.reporting import build_report_payload, render_report_text
from tracking.services.scoring import compute_competitor_baseline, score_items_for_period
from tracking.services.seed_resolver import SeedResolveError, resolve_seed_for_platform


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

    def handle(self, *args, **options):
        entries: list[tuple[str, str]] = []
        entries.extend((Platform.TIKTOK, raw) for raw in (options["tiktok"] or []))
        entries.extend((Platform.INSTAGRAM, raw) for raw in (options["instagram"] or []))
        if not entries:
            raise CommandError("Provide at least one --tiktok or --instagram input")

        user, _ = TgUser.objects.update_or_create(
            tg_user_id=int(options["tg_user_id"]),
            defaults={"tg_chat_id": int(options["tg_user_id"]), "timezone_str": str(options["timezone"] or "UTC")},
        )
        UserCompetitor.objects.filter(user=user).update(is_active=False)

        resolved_rows: list[dict[str, str]] = []
        competitors = []
        for platform, raw in entries:
            try:
                seed = resolve_seed_for_platform(platform=platform, raw_input=raw)
            except SeedResolveError as exc:
                raise CommandError(f"{platform} resolve failed for {raw}: {exc}") from exc
            if seed is None:
                raise CommandError(f"{platform} resolve failed for {raw}: provider did not verify the profile")
            competitor = upsert_competitor(
                user=user,
                platform=seed.platform,
                external_id=seed.external_id,
                handle=seed.handle,
                url=seed.url,
                display_name=seed.title,
                added_by="manual",
                meta={"uploads_playlist_id": seed.uploads_playlist_id} if seed.uploads_playlist_id else None,
            )
            competitors.append(competitor)
            resolved_rows.append(
                {
                    "platform": seed.platform,
                    "input": raw,
                    "external_id": seed.external_id,
                    "handle": seed.handle or "",
                    "url": seed.url,
                }
            )

        period_end = timezone.now()
        period_start = period_end - timedelta(hours=24)

        updated_items = []
        for competitor in competitors:
            updated_items.extend(
                refresh_competitor(
                    competitor=competitor,
                    mode="incremental",
                    captured_at=period_end,
                )
            )

        competitor_by_item_id = {item.id: item.competitor for item in updated_items}
        baseline_by_competitor_id = {
            competitor.id: compute_competitor_baseline(competitor=competitor, now=period_end) for competitor in competitors
        }
        scored = score_items_for_period(
            items=updated_items,
            competitor_by_item_id=competitor_by_item_id,
            baseline_by_competitor_id=baseline_by_competitor_id,
            period_start=period_start,
            period_end=period_end,
        )
        payload = build_report_payload(scored=scored, period_start=period_start, period_end=period_end)

        section_counts = {
            str(section.get("platform")): len(section.get("items") or []) for section in (payload.get("sections") or [])
        }
        for platform in {platform for platform, _ in entries}:
            if section_counts.get(platform, 0) <= 0:
                raise CommandError(
                    f"{platform} verification produced an empty report section with the current provider data and scoring thresholds"
                )

        output = {
            "resolved": resolved_rows,
            "section_counts": section_counts,
            "payload": payload,
            "report_text": render_report_text(payload=payload, timezone_str=str(options["timezone"] or "UTC")),
        }
        self.stdout.write(json.dumps(output, ensure_ascii=False, indent=2))
