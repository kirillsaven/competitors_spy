from __future__ import annotations

from django.db import models


def _default_timezone_str() -> str:
    # Settings can be unavailable at import time (e.g., tooling), so resolve lazily.
    from django.conf import settings

    return getattr(settings, "DEFAULT_TIMEZONE", "Europe/Moscow")


class Platform(models.TextChoices):
    YOUTUBE = "youtube", "YouTube"
    TIKTOK = "tiktok", "TikTok"
    INSTAGRAM = "instagram", "Instagram"


class SeedStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RESOLVED = "resolved", "Resolved"
    FAILED = "failed", "Failed"


class NicheSource(models.TextChoices):
    AUTO = "auto", "Auto"
    MANUAL = "manual", "Manual"


class AddedBy(models.TextChoices):
    MANUAL = "manual", "Manual"
    AUTO = "auto", "Auto"


class LinkedAccountSource(models.TextChoices):
    SEED = "seed", "Seed"
    AUTO = "auto", "Auto"
    MANUAL = "manual", "Manual"


class JobStatus(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"


class ReportStatus(models.TextChoices):
    CREATED = "created", "Created"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class TzSource(models.TextChoices):
    DEFAULT = "default", "Default"
    MANUAL = "manual", "Manual"
    LOCATION = "location", "Location"


class TgUser(models.Model):
    tg_user_id = models.BigIntegerField(unique=True)
    tg_chat_id = models.BigIntegerField()
    tg_username = models.CharField(max_length=255, blank=True, default="")
    tg_first_name = models.CharField(max_length=255, blank=True, default="")
    tg_last_name = models.CharField(max_length=255, blank=True, default="")
    tg_language_code = models.CharField(max_length=32, blank=True, default="")
    timezone_str = models.CharField(max_length=64, default=_default_timezone_str)
    tz_source = models.CharField(max_length=16, choices=TzSource.choices, default=TzSource.DEFAULT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        full_name = " ".join(part for part in [self.tg_first_name, self.tg_last_name] if part).strip()
        if self.tg_username:
            return f"tg:@{self.tg_username}" if not full_name else f"tg:@{self.tg_username} ({full_name})"
        if full_name:
            return f"tg:{self.tg_user_id} ({full_name})"
        return f"tg:{self.tg_user_id}"


class SeedProfile(models.Model):
    user = models.ForeignKey(TgUser, on_delete=models.CASCADE, related_name="seed_profiles")
    raw_input = models.TextField()
    detected_platform = models.CharField(max_length=16, choices=Platform.choices, blank=True, default="")
    canonical_url = models.URLField(blank=True, default="")
    niche_keywords = models.JSONField(default=list, blank=True)
    niche_source = models.CharField(max_length=16, choices=NicheSource.choices, default=NicheSource.MANUAL)
    status = models.CharField(max_length=16, choices=SeedStatus.choices, default=SeedStatus.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"SeedProfile({self.user_id}, {self.detected_platform or 'unknown'})"


class Competitor(models.Model):
    platform = models.CharField(max_length=16, choices=Platform.choices)
    external_id = models.CharField(max_length=128)
    handle = models.CharField(max_length=128, blank=True, default="")
    url = models.URLField(blank=True, default="")
    display_name = models.CharField(max_length=255, blank=True, default="")
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["platform", "external_id"], name="uniq_platform_external"),
        ]

    def __str__(self) -> str:
        return f"{self.platform}:{self.display_name or self.handle or self.external_id}"


class UserCompetitor(models.Model):
    """
    User's tracking list.

    Shared snapshots are stored on Competitor/ContentItem/MetricSnapshot, while this table only stores
    per-user preferences (active flag, origin).
    """

    user = models.ForeignKey(TgUser, on_delete=models.CASCADE, related_name="competitor_links")
    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="user_links")
    added_by = models.CharField(max_length=16, choices=AddedBy.choices, default=AddedBy.MANUAL)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "competitor"], name="uniq_user_competitor"),
        ]

    def __str__(self) -> str:
        return f"UserCompetitor({self.user_id}, {self.competitor_id}, active={self.is_active})"


class UserLinkedAccount(models.Model):
    user = models.ForeignKey(TgUser, on_delete=models.CASCADE, related_name="linked_accounts")
    platform = models.CharField(max_length=16, choices=Platform.choices)
    external_id = models.CharField(max_length=128)
    handle = models.CharField(max_length=128, blank=True, default="")
    url = models.URLField(blank=True, default="")
    display_name = models.CharField(max_length=255, blank=True, default="")
    source = models.CharField(max_length=16, choices=LinkedAccountSource.choices, default=LinkedAccountSource.MANUAL)
    is_seed = models.BooleanField(default=False)
    match_signals = models.JSONField(default=list, blank=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "platform"], name="uniq_user_linked_platform"),
        ]

    def __str__(self) -> str:
        return f"UserLinkedAccount({self.user_id}, {self.platform}, seed={self.is_seed})"


class ContentItem(models.Model):
    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="content_items")
    platform = models.CharField(max_length=16, choices=Platform.choices)
    external_id = models.CharField(max_length=128)
    url = models.URLField()
    title = models.CharField(max_length=500, blank=True, default="")
    description = models.TextField(blank=True, default="")
    published_at = models.DateTimeField()
    duration_seconds = models.IntegerField(null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["platform", "external_id"], name="uniq_platform_content_external"),
        ]

    def __str__(self) -> str:
        return f"{self.platform}:{self.title or self.external_id}"


class MetricSnapshot(models.Model):
    content_item = models.ForeignKey(ContentItem, on_delete=models.CASCADE, related_name="snapshots")
    captured_at = models.DateTimeField(db_index=True)
    views = models.BigIntegerField()
    likes = models.BigIntegerField(null=True, blank=True)
    comments = models.BigIntegerField(null=True, blank=True)
    shares = models.BigIntegerField(null=True, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["content_item", "captured_at"]),
        ]

    def __str__(self) -> str:
        return f"Snapshot({self.content_item_id}, {self.captured_at.isoformat()})"


class CompetitorBaseline(models.Model):
    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="baselines")
    computed_at = models.DateTimeField()
    window_days = models.IntegerField(default=30)
    n_items = models.IntegerField(default=30)
    metrics = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"Baseline({self.competitor_id}, {self.computed_at.date().isoformat()})"


class Schedule(models.Model):
    user = models.OneToOneField(TgUser, on_delete=models.CASCADE, related_name="schedule")
    is_enabled = models.BooleanField(default=True)
    times = models.JSONField(default=list, blank=True)  # ["09:00", "21:00"] in user's timezone
    config_version = models.PositiveIntegerField(default=1)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    # Used to avoid overlapping report jobs for the same user (which can lead to identical periods / missing deltas).
    is_running = models.BooleanField(default=False)
    running_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Schedule({self.user_id}, enabled={self.is_enabled})"


class Report(models.Model):
    user = models.ForeignKey(TgUser, on_delete=models.CASCADE, related_name="reports")
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=ReportStatus.choices, default=ReportStatus.CREATED)
    payload = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"Report({self.user_id}, {self.period_start.date().isoformat()})"


class JobRun(models.Model):
    job_type = models.CharField(max_length=64)
    user = models.ForeignKey(TgUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="job_runs")
    status = models.CharField(max_length=16, choices=JobStatus.choices, default=JobStatus.RUNNING)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    attempts = models.IntegerField(default=0)
    error = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"JobRun({self.job_type}, {self.status})"
