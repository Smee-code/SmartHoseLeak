"""
Data model for the Smart Hose Leak Detection System.

The physical rig is one ESP32 carrying three sensor pairs (flow + pressure).
Each pair monitors one stretch of hose, so a "module" on the device maps to
exactly one Segment row here. Everything else hangs off that mapping.
"""

import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

BAR_TO_PSI = 14.5037738


def psi(bar):
    """Convert bar to PSI. The device reports bar; the paper reports PSI."""
    if bar is None:
        return None
    return round(bar * BAR_TO_PSI, 1)


class User(AbstractUser):
    """One account table for both portals, separated by role."""

    class Role(models.TextChoices):
        FACILITATOR = "facilitator", "Water System Facilitator"
        OWNER = "owner", "Household Owner"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OWNER)
    contact_number = models.CharField(max_length=20, blank=True)
    must_change_password = models.BooleanField(default=False)

    @property
    def is_facilitator(self):
        return self.role == self.Role.FACILITATOR or self.is_superuser

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"


class Device(models.Model):
    """An ESP32 board. The api_key authenticates its POSTs to /api/ingest/."""

    device_id = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=120, blank=True)
    api_key = models.CharField(max_length=64, unique=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    firmware_note = models.CharField(max_length=200, blank=True)

    def save(self, *args, **kwargs):
        if not self.api_key:
            self.api_key = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    @property
    def is_online(self):
        if not self.last_seen:
            return False
        return (timezone.now() - self.last_seen).total_seconds() < 60

    def __str__(self):
        return self.label or self.device_id


class Segment(models.Model):
    """
    One monitored stretch of hose = one module on the ESP32.

    start_m / end_m are the distance markers measured from the main valve.
    A leak flagged on this module is reported as lying between them, which is
    the honest resolution a three-sensor rig can actually deliver.
    """

    class Status(models.TextChoices):
        NORMAL = "NORMAL", "Normal"
        LOW_SUPPLY = "LOW_SUPPLY", "Low Water Supply"
        POSSIBLE_LEAK = "POSSIBLE_LEAK", "Possible Leak"
        MAINTENANCE = "MAINTENANCE", "Under Maintenance"
        OFFLINE = "OFFLINE", "Offline"

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="segments")
    module_index = models.PositiveSmallIntegerField(help_text="Module number on the ESP32 (1-3).")
    name = models.CharField(max_length=120)
    start_m = models.FloatField(default=0, help_text="Metres from the main valve where this segment begins.")
    end_m = models.FloatField(default=15, help_text="Metres from the main valve where this segment ends.")

    # Optional geography. When these are set the dashboard can draw the run on
    # a real Leaflet map; when blank, the schematic view is used instead.
    start_lat = models.FloatField(null=True, blank=True)
    start_lng = models.FloatField(null=True, blank=True)
    end_lat = models.FloatField(null=True, blank=True)
    end_lng = models.FloatField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OFFLINE)
    status_changed_at = models.DateTimeField(default=timezone.now)
    maintenance_note = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["module_index"]
        constraints = [
            models.UniqueConstraint(fields=["device", "module_index"], name="unique_module_per_device"),
        ]

    @property
    def length_m(self):
        return round(self.end_m - self.start_m, 2)

    @property
    def location_label(self):
        return f"between {self.start_m:g} m and {self.end_m:g} m from the main valve"

    def __str__(self):
        return f"M{self.module_index} - {self.name}"


class LatestReading(models.Model):
    """The live value per segment, overwritten on every ingest."""

    segment = models.OneToOneField(Segment, on_delete=models.CASCADE, related_name="latest")
    pressure_bar = models.FloatField(default=0)
    flow_lpm = models.FloatField(default=0)
    k_conductance = models.FloatField(default=0)
    device_status = models.CharField(max_length=20, default="NORMAL")
    calibrating = models.BooleanField(default=False)
    simulated = models.BooleanField(default=False)
    updated_at = models.DateTimeField(default=timezone.now)

    @property
    def pressure_psi(self):
        return psi(self.pressure_bar)

    @property
    def is_fresh(self):
        return (timezone.now() - self.updated_at).total_seconds() < 60

    def __str__(self):
        return f"{self.segment} at {self.pressure_bar:.2f} bar"


class ReadingRollup(models.Model):
    """
    One averaged row per segment per minute.

    The device sends three readings every second. Storing every raw sample
    would be roughly 260k rows a day, so samples are folded into the current
    minute bucket as they arrive and the history charts read these instead.
    """

    segment = models.ForeignKey(Segment, on_delete=models.CASCADE, related_name="rollups")
    minute = models.DateTimeField(db_index=True)
    avg_pressure_bar = models.FloatField(default=0)
    min_pressure_bar = models.FloatField(default=0)
    max_pressure_bar = models.FloatField(default=0)
    avg_flow_lpm = models.FloatField(default=0)
    min_flow_lpm = models.FloatField(default=0)
    max_flow_lpm = models.FloatField(default=0)
    sample_count = models.PositiveIntegerField(default=0)
    leak_samples = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-minute"]
        constraints = [
            models.UniqueConstraint(fields=["segment", "minute"], name="unique_segment_minute"),
        ]

    def fold(self, pressure_bar, flow_lpm, is_leak):
        """Merge one raw sample into this bucket without storing the sample."""
        n = self.sample_count
        self.avg_pressure_bar = (self.avg_pressure_bar * n + pressure_bar) / (n + 1)
        self.avg_flow_lpm = (self.avg_flow_lpm * n + flow_lpm) / (n + 1)
        if n == 0:
            self.min_pressure_bar = self.max_pressure_bar = pressure_bar
            self.min_flow_lpm = self.max_flow_lpm = flow_lpm
        else:
            self.min_pressure_bar = min(self.min_pressure_bar, pressure_bar)
            self.max_pressure_bar = max(self.max_pressure_bar, pressure_bar)
            self.min_flow_lpm = min(self.min_flow_lpm, flow_lpm)
            self.max_flow_lpm = max(self.max_flow_lpm, flow_lpm)
        self.sample_count = n + 1
        if is_leak:
            self.leak_samples += 1

    def __str__(self):
        return f"{self.segment} {self.minute:%Y-%m-%d %H:%M}"


class Household(models.Model):
    """A registered water subscriber, maintained by the facilitator."""

    class AccountStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        DISCONNECTED = "disconnected", "Disconnected"

    household_id = models.CharField(max_length=32, unique=True)
    owner_name = models.CharField(max_length=120)
    address = models.CharField(max_length=255)
    contact_number = models.CharField(max_length=20)
    segment = models.ForeignKey(
        Segment, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="households", help_text="Supply line assignment.",
    )
    account_status = models.CharField(
        max_length=20, choices=AccountStatus.choices, default=AccountStatus.ACTIVE
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="household",
    )
    date_registered = models.DateField(default=timezone.localdate)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["household_id"]

    def __str__(self):
        return f"{self.household_id} - {self.owner_name}"


class LeakEvent(models.Model):
    """
    One row per debounced status change on a segment.

    The ESP32 already applies its own hysteresis (REQUIRED_CONSECUTIVE
    samples), so anything arriving here as a changed status is a confirmed
    transition and is recorded verbatim.
    """

    class Severity(models.TextChoices):
        INFO = "info", "Informational"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    segment = models.ForeignKey(Segment, on_delete=models.CASCADE, related_name="events")
    status = models.CharField(max_length=20, choices=Segment.Status.choices)
    previous_status = models.CharField(max_length=20, choices=Segment.Status.choices, blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.INFO)
    pressure_bar = models.FloatField(default=0)
    flow_lpm = models.FloatField(default=0)
    location_from_m = models.FloatField(default=0)
    location_to_m = models.FloatField(default=0)
    detected_at = models.DateTimeField(default=timezone.now, db_index=True)
    cleared_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="acknowledged_events",
    )
    resolution_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-detected_at"]

    @property
    def is_open(self):
        return self.cleared_at is None

    @property
    def location_label(self):
        return f"{self.location_from_m:g}-{self.location_to_m:g} m from main valve"

    def __str__(self):
        return f"{self.segment} {self.status} @ {self.detected_at:%Y-%m-%d %H:%M}"


class Notification(models.Model):
    """
    Facilitator to owner messaging, plus the automatic leak notices raised by
    the ingest endpoint for households on an affected supply line.
    """

    class Kind(models.TextChoices):
        AUTOMATIC = "automatic", "Automatic Alert"
        TARGETED = "targeted", "Targeted Notice"
        BROADCAST = "broadcast", "Broadcast"

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.TARGETED)
    title = models.CharField(max_length=150)
    body = models.TextField()
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="sent_notifications",
    )
    recipient = models.ForeignKey(
        Household, on_delete=models.CASCADE, null=True, blank=True, related_name="notifications",
        help_text="Blank means every active household (broadcast).",
    )
    segment = models.ForeignKey(
        Segment, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications"
    )
    event = models.ForeignKey(
        LeakEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications"
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class Complaint(models.Model):
    """A water supply complaint filed from the Owner Portal."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In Progress"
        RESOLVED = "resolved", "Resolved"
        REJECTED = "rejected", "Rejected"

    class Category(models.TextChoices):
        NO_WATER = "no_water", "No Water Supply"
        LOW_PRESSURE = "low_pressure", "Low Water Pressure"
        VISIBLE_LEAK = "visible_leak", "Visible Leak"
        WATER_QUALITY = "water_quality", "Water Quality"
        OTHER = "other", "Other"

    reference = models.CharField(max_length=20, unique=True, blank=True)
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="complaints")
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    subject = models.CharField(max_length=150)
    body = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    response = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="complaint_responses",
    )

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"CMP-{timezone.now():%y%m%d}-{secrets.token_hex(2).upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} - {self.subject}"
