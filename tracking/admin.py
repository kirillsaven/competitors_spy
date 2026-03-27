from __future__ import annotations

from django.contrib import admin

from .models import (
    Competitor,
    CompetitorBaseline,
    ContentItem,
    JobRun,
    MetricSnapshot,
    Report,
    Schedule,
    SeedProfile,
    TgUser,
    UserLinkedAccount,
    UserCompetitor,
)


@admin.register(TgUser)
class TgUserAdmin(admin.ModelAdmin):
    list_display = (
        "tg_user_id",
        "tg_username",
        "tg_first_name",
        "tg_last_name",
        "tg_language_code",
        "tg_chat_id",
        "timezone_str",
        "tz_source",
        "created_at",
        "updated_at",
    )
    search_fields = ("tg_user_id", "tg_username", "tg_first_name", "tg_last_name", "tg_chat_id", "timezone_str")
    list_filter = ("tz_source",)


@admin.register(SeedProfile)
class SeedProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "detected_platform", "status", "niche_source", "created_at")
    search_fields = ("raw_input", "canonical_url")
    list_filter = ("detected_platform", "status", "niche_source")


@admin.register(Competitor)
class CompetitorAdmin(admin.ModelAdmin):
    list_display = ("id", "platform", "external_id", "handle", "display_name", "created_at", "updated_at")
    search_fields = ("external_id", "handle", "display_name", "url")
    list_filter = ("platform",)


@admin.register(UserCompetitor)
class UserCompetitorAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "platform", "competitor", "added_by", "is_active", "created_at", "updated_at")
    search_fields = ("user__tg_user_id", "competitor__external_id", "competitor__handle", "competitor__display_name")
    list_filter = ("added_by", "is_active", "competitor__platform")
    list_select_related = ("user", "competitor")

    @admin.display(description="Platform")
    def platform(self, obj: UserCompetitor) -> str:
        return obj.competitor.platform


@admin.register(UserLinkedAccount)
class UserLinkedAccountAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "platform", "external_id", "handle", "display_name", "source", "is_seed")
    search_fields = ("user__tg_user_id", "external_id", "handle", "display_name", "url")
    list_filter = ("platform", "source", "is_seed")


@admin.register(ContentItem)
class ContentItemAdmin(admin.ModelAdmin):
    list_display = ("id", "competitor", "platform", "external_id", "published_at", "title", "created_at")
    search_fields = ("external_id", "title", "url")
    list_filter = ("platform",)


@admin.register(MetricSnapshot)
class MetricSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "content_item", "captured_at", "views", "likes", "comments")
    list_filter = ("captured_at",)
    search_fields = ("content_item__external_id", "content_item__title")


@admin.register(CompetitorBaseline)
class CompetitorBaselineAdmin(admin.ModelAdmin):
    list_display = ("id", "competitor", "computed_at", "window_days", "n_items")
    list_filter = ("computed_at",)


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "is_enabled",
        "is_running",
        "running_started_at",
        "times",
        "next_run_at",
        "last_run_at",
        "created_at",
        "updated_at",
    )
    list_filter = ("is_enabled",)


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "period_start", "period_end", "status", "created_at", "sent_at")
    list_filter = ("status",)


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin):
    list_display = ("id", "job_type", "user", "status", "started_at", "finished_at", "attempts")
    list_filter = ("job_type", "status")
    search_fields = ("error",)
