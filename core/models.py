"""
Data model for the Smart Hose Leak Detection System.

The physical rig is one ESP32 carrying three sensor pairs (flow + pressure).
Each pair monitors one stretch of hose, so a "module" on the device maps to
exactly one Segment row here. Everything else hangs off that mapping.
"""

import re
import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone

BAR_TO_PSI = 14.5037738


def psi(bar):
    """Convert bar to PSI. The device reports bar; the paper reports PSI."""
    if bar is None:
        return None
    return round(bar * BAR_TO_PSI, 1)


class Barangay(models.Model):
    """
    One barangay served by the system.

    The project began in a single barangay (Lower Dakong Patag), and for a
    long time "the barangay" was a line of settings text, not a record.
    Supporting more than one - each with its own facilitator, households and
    hose lines - is what makes a system administrator meaningful: someone
    above the facilitators who can see and manage every barangay at once,
    while each facilitator still works only within their own.
    """

    name = models.CharField(max_length=120, unique=True)
    municipality = models.CharField(max_length=120, default="Sison")
    province = models.CharField(max_length=120, default="Surigao del Norte")
    # Deactivated rather than deleted: a barangay with households and readings
    # behind it should never vanish from the records because it stopped being
    # served. Inactive ones drop out of the pickers but keep their history.
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        verbose_name = "Barangay"
        verbose_name_plural = "Barangays"

    @property
    def location_label(self):
        return f"Brgy. {self.name}, {self.municipality}, {self.province}"

    @classmethod
    def default(cls):
        """
        The barangay this installation serves.

        The system serves one barangay, so nothing asks which; records are
        attached to this one. The model still allows several - this is the
        one place that decides "which", so switching that back on later is a
        matter of asking here instead of assuming. Created on first use so a
        fresh database works without a fixture.
        """
        barangay = cls.objects.filter(is_active=True).order_by("pk").first()
        if barangay is None:
            barangay, _ = cls.objects.get_or_create(
                name=settings.DEFAULT_BARANGAY_NAME,
                defaults={"municipality": "Sison", "province": "Surigao del Norte"},
            )
        return barangay

    def __str__(self):
        return self.name


class User(AbstractUser):
    """One account table for every portal, separated by role."""

    class Role(models.TextChoices):
        ADMIN = "admin", "System Administrator"
        FACILITATOR = "facilitator", "Water System Facilitator"
        OWNER = "owner", "Household Owner"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OWNER)
    contact_number = models.CharField(max_length=20, blank=True)
    must_change_password = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)

    # Which barangay a facilitator is responsible for. Null for an admin, who
    # oversees every barangay, and for a household owner, whose barangay is
    # carried on their household record instead of here.
    barangay = models.ForeignKey(
        "Barangay", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="facilitators",
        help_text="The barangay this facilitator manages.",
    )

    # A facilitator's own reference number (FAC-001), given out in sequence
    # the way households get HH-001. Blank for admins and owners.
    FAC_ID_PREFIX = "FAC-"
    FAC_ID_DIGITS = 3
    facilitator_id = models.CharField(
        max_length=12, blank=True, null=True, unique=True,
        help_text="Assigned automatically to facilitator accounts.",
    )

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN or self.is_superuser

    @property
    def is_facilitator(self):
        # Admin and facilitator are separate roles: an admin is not a
        # facilitator and cannot open the facilitator dashboard, just as a
        # facilitator cannot open the admin console. Each sees only its own
        # side. (A Django superuser counts as an admin, above, not here.)
        return self.role == self.Role.FACILITATOR

    @classmethod
    def next_facilitator_id(cls):
        """The next free FAC- number, from the highest one in use."""
        pattern = re.compile(rf"^{re.escape(cls.FAC_ID_PREFIX)}(\d+)$")
        highest = 0
        for existing in cls.objects.exclude(facilitator_id__isnull=True).values_list("facilitator_id", flat=True):
            match = pattern.match(existing or "")
            if match:
                highest = max(highest, int(match.group(1)))
        return f"{cls.FAC_ID_PREFIX}{highest + 1:0{cls.FAC_ID_DIGITS}d}"

    def save(self, *args, **kwargs):
        # Every facilitator gets a number on first save; nobody types one.
        if self.role == self.Role.FACILITATOR and not self.facilitator_id:
            self.facilitator_id = self.next_facilitator_id()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"


class Device(models.Model):
    """
    An ESP32 board. The api_key authenticates its POSTs to /api/ingest/.

    Both the id and the key are issued here and never typed. The server is
    the record of what a board is; the board holds a copy it was given. That
    direction matters: a key a human can invent is a key a human can collide
    with, mistype, or reuse across two boards - and two boards reporting as
    the same household overwrite each other's readings silently.

    The key stays readable on the facilitator side rather than being shown
    once. When a board dies and a spare is flashed, the key has to be typed
    into it again, and an owner password can be reset where a device key
    cannot - regenerating one means walking back to the house.
    """

    ID_PREFIX = "esp32-"
    ID_DIGITS = 2

    device_id = models.CharField(
        max_length=64, unique=True, blank=True,
        help_text="Assigned automatically (esp32-01, esp32-02, ...) when left blank.",
    )
    label = models.CharField(max_length=120, blank=True)
    api_key = models.CharField(
        max_length=64, unique=True, blank=True,
        help_text="Generated by the server. Never type one in.",
    )
    last_seen = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    firmware_note = models.CharField(max_length=200, blank=True)

    def save(self, *args, **kwargs):
        if not self.api_key:
            self.api_key = secrets.token_urlsafe(32)
        if not self.device_id:
            self.device_id = self.next_device_id()
        super().save(*args, **kwargs)

    @classmethod
    def next_device_id(cls):
        """
        The next free esp32-NN.

        Counts from the highest number in use rather than from how many rows
        exist, so retiring a board does not hand its id to the next one. Two
        boards sharing an id at different times makes the event log lie.
        """
        highest = 0
        for existing in cls.objects.values_list("device_id", flat=True):
            match = re.fullmatch(rf"{re.escape(cls.ID_PREFIX)}(\d+)", existing or "")
            if match:
                highest = max(highest, int(match.group(1)))
        return f"{cls.ID_PREFIX}{highest + 1:0{cls.ID_DIGITS}d}"

    def issue_claim_code(self):
        """
        A fresh code for this board, replacing any earlier one.

        Replacing rather than adding: two live codes for one board means two
        people can claim it, and the second silently takes over the first
        board's identity.
        """
        ClaimCode.objects.filter(device=self).delete()
        return ClaimCode.objects.create(device=self)

    def regenerate_key(self):
        """
        Issue a new key and invalidate the old one.

        Deliberately a method rather than an edit: the board goes silent the
        moment this runs and stays silent until somebody walks to it and
        enters the new key. That is a decision, not a field change.
        """
        self.api_key = secrets.token_urlsafe(32)
        self.save(update_fields=["api_key"])
        return self.api_key

    @property
    def line(self):
        """The hose line this board is fitted to, if any."""
        return self.segments.first()

    @property
    def is_online(self):
        # Same window the dashboards use to call a reading stale, so the device
        # badge and the per-line status can never contradict each other.
        if not self.last_seen:
            return False
        return (timezone.now() - self.last_seen).total_seconds() < settings.DEVICE_OFFLINE_AFTER_SECONDS

    def __str__(self):
        return self.label or self.device_id


class ClaimCode(models.Model):
    """
    A short, single-use ticket that a blank board trades for its identity.

    The alternative is typing the API key into the board by hand: 43
    mixed-case characters with dashes and underscores, on a phone, standing
    in somebody's yard. A mistyped key gives a 401, and a 401 on the serial
    monitor looks exactly like a wrong server address - so the failure is
    both likely and expensive to diagnose.

    A code is worth nothing once used, which is why it can be short enough to
    read aloud. The key itself never reaches the phone and never leaves the
    server except to the board that claimed it.
    """

    # No 0/O or 1/I/L. The code gets read off a screen and typed on a phone,
    # and those pairs are the ones people get wrong.
    ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
    GROUP = 3
    GROUPS = 2
    VALID_DAYS = 14

    code = models.CharField(max_length=16, unique=True, db_index=True)
    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="claim_code")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_from_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = self.generate()
        if not self.expires_at:
            self.expires_at = timezone.now() + timezone.timedelta(days=self.VALID_DAYS)
        super().save(*args, **kwargs)

    @classmethod
    def generate(cls):
        """A code no other row is using, formatted as K7M-4Q2."""
        while True:
            groups = [
                "".join(secrets.choice(cls.ALPHABET) for _ in range(cls.GROUP))
                for _ in range(cls.GROUPS)
            ]
            code = "-".join(groups)
            if not cls.objects.filter(code=code).exists():
                return code

    @classmethod
    def normalise(cls, raw):
        """
        Accept what a person actually types.

        Lower case, missing dashes and stray spaces are all the same code as
        far as anyone entering it is concerned, and rejecting them would look
        like the code was wrong rather than formatted differently.
        """
        cleaned = "".join(ch for ch in (raw or "").upper() if ch in cls.ALPHABET)
        if len(cleaned) != cls.GROUP * cls.GROUPS:
            return ""
        return "-".join(
            cleaned[i:i + cls.GROUP] for i in range(0, len(cleaned), cls.GROUP)
        )

    @property
    def is_used(self):
        return self.used_at is not None

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_usable(self):
        return not self.is_used and not self.is_expired

    @property
    def state_label(self):
        if self.is_used:
            return "claimed"
        if self.is_expired:
            return "expired"
        return "waiting"

    def __str__(self):
        return f"{self.code} ({self.state_label})"


class Segment(models.Model):
    """
    One household's hose line: main source -> that house, with its own device.

    The rig started as three sensor pairs sharing one ESP32 along a single
    run, which is why a line used to carry distance markers and a leak was
    reported as lying between them. That only ever gave the facilitator a
    stretch of hose to walk.

    Each house now has its own hose from the source and its own board, so a
    leak is attributed to a household instead of to a position. That is both
    more useful - it names the door to knock on - and more honest, because
    two sensors at one end of a pipe cannot locate a hole along it.
    """

    class Status(models.TextChoices):
        NORMAL = "NORMAL", "Normal"
        LOW_SUPPLY = "LOW_SUPPLY", "Low Water Supply"
        WATER_RUNNING = "WATER_RUNNING", "Water Running"
        POSSIBLE_LEAK = "POSSIBLE_LEAK", "Possible Leak"
        MAINTENANCE = "MAINTENANCE", "Under Maintenance"
        OFFLINE = "OFFLINE", "Offline"

    device = models.ForeignKey(
        Device, on_delete=models.SET_NULL, null=True, blank=True, related_name="segments",
        help_text="The board fitted to this line. Blank until one is installed.",
    )
    module_index = models.PositiveSmallIntegerField(
        default=1, help_text="Sensor module on the board. One per board now, so always 1."
    )
    name = models.CharField(max_length=120)

    # Which barangay this line runs in. A line belongs to one household, which
    # also carries the barangay, but it is stored here as well so a line laid
    # before its household is registered is still attributed to the right
    # place - and so the leak map can be filtered by barangay without joining
    # back through the household every time.
    barangay = models.ForeignKey(
        "Barangay", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="segments",
    )

    # Optional geography. When these are set the dashboard can draw the run on
    # a real Leaflet map; when blank, the schematic view is used instead.
    start_lat = models.FloatField(null=True, blank=True)
    start_lng = models.FloatField(null=True, blank=True)
    end_lat = models.FloatField(null=True, blank=True)
    end_lng = models.FloatField(null=True, blank=True)

    # What counts as "too little water" on THIS line, in bar.
    #
    # It lives here rather than in the firmware because the right value is a
    # property of the site, not of the code: a gravity-fed line sits at
    # roughly 1 bar per 10 m the source is above it, so a line further down
    # the hill is normally weaker than one near the tank and a single figure
    # shared by all three would either nuisance-alarm on one or go blind on
    # another. Keeping it per line, and editable here, means tuning the
    # system to a barangay never means re-flashing the board.
    #
    # 0.3 bar is about 3 m of head, the minimum residual pressure rural
    # gravity systems are designed around - a defensible starting point, but
    # replace it with a figure measured on the line once one exists.
    low_pressure_bar = models.FloatField(
        default=0.3,
        validators=[MinValueValidator(0.0), MaxValueValidator(12.0)],
        verbose_name="Low supply below",
        help_text=(
            "Pressure in bar under which this line is reported as Low Water Supply. "
            "Sent to the device automatically - no re-flashing needed."
        ),
    )

    # A one-shot request for the device to run its sealed-line pressure test.
    #
    # The board is not reachable from here - it sits behind the barangay's
    # router and dials out - so a command cannot be pushed to it. Instead the
    # request is parked here and handed over on the reply to the board's next
    # upload, which is at most a couple of seconds away. The board clears it
    # by reporting that the valve has started moving, so a request survives a
    # dropped reply instead of being lost the moment it is sent.
    valve_test_requested = models.BooleanField(default=False)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OFFLINE)
    status_changed_at = models.DateTimeField(default=timezone.now)
    maintenance_note = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)

    # How this line and its households are drawn on the leak map. Kept apart
    # from the status palette so line identity is never mistaken for an alarm.
    COLOR_CHOICES = [
        ("#0891b2", "Cyan"),
        ("#4338ca", "Indigo"),
        ("#be185d", "Pink"),
        ("#0f766e", "Teal"),
        ("#c2410c", "Burnt orange"),
        ("#4d7c0f", "Olive"),
        ("#7e22ce", "Purple"),
        ("#1e293b", "Slate"),
    ]
    DEFAULT_COLOR_CYCLE = [c for c, _ in COLOR_CHOICES]

    color = models.CharField(
        max_length=7, choices=COLOR_CHOICES, default="#0891b2",
        help_text="Colour used for this line and its households on the leak map.",
    )

    class Meta:
        ordering = ["module_index"]
        constraints = [
            models.UniqueConstraint(fields=["device", "module_index"], name="unique_module_per_device"),
        ]

    @classmethod
    def next_module_index(cls, device):
        """
        The next free module number on this device.

        Uses the highest in use rather than a count, so deleting a line from
        the middle does not hand its module number to a new one while the
        firmware is still reporting against it.
        """
        highest = cls.objects.filter(device=device).aggregate(
            top=models.Max("module_index")
        )["top"]
        return (highest or 0) + 1

    @classmethod
    def next_color(cls, device=None):
        """
        First palette colour no other line is using.

        Compared across every line, not per device: the colours exist so the
        hoses can be told apart on one map, and two identical branches would
        defeat that however they are wired.
        """
        taken = set(cls.objects.values_list("color", flat=True))
        for candidate in cls.DEFAULT_COLOR_CYCLE:
            if candidate not in taken:
                return candidate
        return cls.DEFAULT_COLOR_CYCLE[0]  # more lines than colours: reuse


    @property
    def low_pressure_psi(self):
        """The same threshold in psi, for the gauges people actually own."""
        return round(self.low_pressure_bar * BAR_TO_PSI, 1)

    @property
    def location_label(self):
        """
        Where a leak on this line is. The household, not a distance.

        Falls back to the line's own name while it has no household, so an
        alert raised during installation still says something useful.
        """
        household = getattr(self, "household", None)
        if household is None:
            return f"on {self.name}"
        return f"on {household.household_id}'s line ({household.owner_name})"

    @property
    def has_device(self):
        """
        Whether real hardware is fitted.

        A household can be registered long before a board reaches it, and the
        two states must not look alike: a line waiting for hardware is part
        of the rollout, while a line whose board has stopped reporting is a
        fault someone has to go and fix.
        """
        return self.device_id is not None

    def __str__(self):
        return self.name


class ValveState(models.TextChoices):
    """
    What the motorised ball valve on the line is doing.

    NOT_FITTED is deliberately distinct from UNKNOWN. A line with no valve is
    working as designed and needs nothing; a line whose valve state cannot be
    read is a fault. Collapsing the two would hide the second behind the
    first for every household that has not been upgraded yet.
    """

    NOT_FITTED = "not_fitted", "Not fitted"
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed (testing)"
    MOVING = "moving", "Moving"
    UNKNOWN = "unknown", "Unknown"


class DeviceSeverity(models.TextChoices):
    """
    How bad the device thinks the leak is, from flow rate or decay speed.

    Distinct from LeakEvent.Severity, which grades how much attention the
    event needs. A minor drip can still be a warning worth acting on, so the
    two are related but not the same scale.
    """

    NONE = "none", "None"
    MINOR = "minor", "Minor"
    MODERATE = "moderate", "Moderate"
    MAJOR = "major", "Major"


class Trigger(models.TextChoices):
    """
    Which of the device's tests fired.

    Carried through to the dashboard because the tests answer different
    questions, and the right response differs: a night-flow flag on a line
    the family swears was idle is strong evidence, while sustained low flow
    is just as likely to be a tap left open - which is why that alert asks
    the owner to check their faucets before anyone is sent out.
    """

    NONE = "none", "-"
    BURST = "burst", "Burst (flow spike + pressure crash)"
    SUSTAINED = "sustained", "Sustained low flow"
    NIGHT = "night", "Night flow"
    DECAY = "decay", "Valve-closed pressure decay"
    LOW_SUPPLY = "low_supply", "Low supply"


class LatestReading(models.Model):
    """The live value per segment, overwritten on every ingest."""

    segment = models.OneToOneField(Segment, on_delete=models.CASCADE, related_name="latest")
    pressure_bar = models.FloatField(default=0)
    flow_lpm = models.FloatField(default=0)
    k_conductance = models.FloatField(default=0)
    device_status = models.CharField(max_length=20, default="NORMAL")

    # Sent by firmware that supports them; the defaults keep a board running
    # the older sketch reporting normally instead of failing to ingest.
    severity = models.CharField(max_length=10, choices=DeviceSeverity.choices, default=DeviceSeverity.NONE)
    trigger = models.CharField(max_length=12, choices=Trigger.choices, default=Trigger.NONE)
    valve_state = models.CharField(
        max_length=12, choices=ValveState.choices, default=ValveState.NOT_FITTED
    )

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

    ID_PREFIX = "HH-"
    ID_DIGITS = 3

    class AccountStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        DISCONNECTED = "disconnected", "Disconnected"

    household_id = models.CharField(
        max_length=32, unique=True, blank=True,
        help_text="Assigned automatically (HH-001, HH-002, ...) when left blank.",
    )
    owner_name = models.CharField(max_length=120)
    address = models.CharField(max_length=255)
    contact_number = models.CharField(max_length=20)
    # Optional. Where sign-in codes and notices go when the household would
    # rather have email than a text; copied onto the owner's login when one
    # is created. Most households here have a number and no email, so blank
    # is the common and perfectly fine state.
    email = models.EmailField(blank=True)
    # Which barangay this household is in. Nullable so existing records survive
    # the move to multiple barangays; a data migration stamps them all with the
    # founding barangay, and new households are given one when registered.
    barangay = models.ForeignKey(
        "Barangay", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="households",
    )
    # One hose runs from the main source to this house and nowhere else, so
    # the line and the household are the same thing seen from two ends. Kept
    # nullable because a household is registered before its hose is built.
    segment = models.OneToOneField(
        Segment, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="household", help_text="This household's hose line.",
    )

    # Optional, so a household can be registered before anyone has stood at the
    # tap with a phone. Both must be present for the pin to be drawn.
    latitude = models.FloatField(
        null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
        help_text="Decimal degrees, e.g. 9.7167. Leave blank if not yet surveyed.",
    )
    longitude = models.FloatField(
        null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
        help_text="Decimal degrees, e.g. 125.5333.",
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
    is_archived = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)

    # Two people make a household real. The administrator enters the record
    # at application; the facilitator activates it - confirms where the
    # house is and creates the owner's login. Until then it is pending.
    activated_at = models.DateTimeField(null=True, blank=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="activated_households",
    )

    @property
    def is_pending(self):
        """Entered by the administrator, not yet activated by the facilitator."""
        return self.user_id is None and self.activated_at is None

    @property
    def device_setup(self):
        """Where this household's board is in its setup, as a dict (core.setup)."""
        from . import setup
        return setup.device_setup(self)

    # The road-following route from the barangay's water source to this
    # house, for visits - as distinct from the hose, which goes cross-country.
    # Fetched once from the routing service when the coordinates are saved
    # and kept here, so the map draws it with no internet and no further
    # calls. A list of [lat, lng] points; None until there is one. The error
    # is the short reason the last attempt did not produce a route, if any.
    visit_route = models.JSONField(null=True, blank=True)
    visit_route_distance_m = models.FloatField(null=True, blank=True)
    visit_route_duration_s = models.FloatField(null=True, blank=True)
    visit_route_fetched_at = models.DateTimeField(null=True, blank=True)
    visit_route_error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["household_id"]

    @classmethod
    def next_household_id(cls):
        """
        The next free sequential id, e.g. HH-007.

        Taken from the highest existing number rather than the row count, so
        removing a household from the middle of the range does not cause the
        next one to collide with an id already in use.

        Note it is the highest *existing* id: delete the newest household and
        its number is handed out again. That is fine while records are only
        removed by mistake, but if ids ever end up on printed receipts, this
        should become a stored counter instead.

        Ids that do not match the pattern are ignored, so one typed by hand
        cannot break the sequence.
        """
        pattern = re.compile(rf"^{re.escape(cls.ID_PREFIX)}(\d+)$")
        highest = 0
        for existing in cls.objects.values_list("household_id", flat=True):
            match = pattern.match(existing or "")
            if match:
                highest = max(highest, int(match.group(1)))
        return f"{cls.ID_PREFIX}{highest + 1:0{cls.ID_DIGITS}d}"

    def save(self, *args, **kwargs):
        """
        Assign an id on first save when one was not supplied.

        The retry covers the gap between reading the highest id and writing
        the row: two facilitators saving at the same moment would otherwise
        both be handed the same number and the second would fail.
        """
        if self.household_id:
            return super().save(*args, **kwargs)

        for attempt in range(5):
            self.household_id = self.next_household_id()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if attempt == 4:
                    raise
                # Force an INSERT on the next attempt rather than an UPDATE
                # against a primary key the failed save may have set.
                self.pk = None

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def navigate_url(self):
        """
        Turn-by-turn directions to this house in the phone's maps app.

        Google's directions link needs no key and opens the app people here
        already have, which is what actually guides someone to the door - the
        drawn route is the overview, this is the last stretch. Starts from
        the barangay's water source when that has a location, otherwise from
        wherever the phone is.
        """
        if not self.has_location:
            return None
        source = getattr(self.barangay, "water_source", None) if self.barangay_id else None
        origin = (
            f"&origin={source.latitude},{source.longitude}"
            if source is not None and source.has_location else ""
        )
        return (
            "https://www.google.com/maps/dir/?api=1"
            f"{origin}&destination={self.latitude},{self.longitude}&travelmode=driving"
        )

    @property
    def visit_route_label(self):
        """'1.2 km, about 4 min' - what the stored route amounts to, or ''."""
        if not self.visit_route:
            return ""
        parts = []
        if self.visit_route_distance_m:
            metres = self.visit_route_distance_m
            parts.append(f"{metres / 1000:.1f} km" if metres >= 1000 else f"{metres:.0f} m")
        if self.visit_route_duration_s:
            parts.append(f"about {max(1, round(self.visit_route_duration_s / 60))} min")
        return ", ".join(parts)

    def __str__(self):
        return f"{self.household_id} - {self.owner_name}"


class WaterSource(models.Model):
    """
    Where every household's hose begins: the spring box, tank or main tap.

    Distance markers on every segment are measured from this point, so it is
    the anchor the whole system's leak locations are expressed against. A
    single row - there is one main line per installation - kept as a model
    rather than a setting so the facilitator can move it from the dashboard
    without anyone editing a file.
    """

    name = models.CharField(
        max_length=120, default="Main source",
        help_text="What is at this point, e.g. 'Main source', 'Spring box' or 'Reservoir'.",
    )
    latitude = models.FloatField(
        null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
        help_text="Decimal degrees, e.g. 9.6620.",
    )
    longitude = models.FloatField(
        null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
        help_text="Decimal degrees, e.g. 125.5290.",
    )
    notes = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    # One source per barangay. The system began with a single row for a
    # single barangay; each barangay has its own spring box or tank, and its
    # facilitator edits only theirs. Nullable only so the founding row
    # survives the migration that attaches it to its barangay.
    barangay = models.OneToOneField(
        "Barangay", on_delete=models.CASCADE, null=True, blank=True,
        related_name="water_source",
    )

    class Meta:
        verbose_name = "water source"
        verbose_name_plural = "water sources"

    @classmethod
    def for_barangay(cls, barangay):
        """That barangay's source, created on first use so no fixture is needed."""
        if barangay is None:
            return None
        source, _ = cls.objects.get_or_create(barangay=barangay)
        return source

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None

    def __str__(self):
        return self.name


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

    # Which test fired and how bad the device judged it. Stored on the event
    # rather than looked up later, because the live reading has moved on by
    # the time anybody opens the log.
    device_severity = models.CharField(max_length=10, choices=DeviceSeverity.choices, default=DeviceSeverity.NONE)
    trigger = models.CharField(max_length=12, choices=Trigger.choices, default=Trigger.NONE)

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
        """
        Which household this event belongs to.

        Reads off the segment rather than being copied in, so renaming a
        household does not leave old alerts naming the previous owner.
        """
        return self.segment.location_label

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
    # Set on notices that mirror a complaint reply, so the notice can link
    # straight to the conversation instead of being a dead copy of it.
    # CASCADE: if the thread is deleted, a notice pointing into it is noise.
    complaint = models.ForeignKey(
        "Complaint", on_delete=models.CASCADE, null=True, blank=True,
        related_name="notifications",
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


class SmsMessage(models.Model):
    """
    One text message, queued here and sent by the worker.

    Queued rather than sent inline because a SIM800L takes several seconds to
    negotiate with the network, and a facilitator must never sit watching a
    spinner because a modem is busy. It also means an outage is survivable:
    the message waits in the database instead of vanishing with the request
    that raised it.

    The number is copied in at queue time rather than followed through to the
    household. A log has to record where a message actually went, and a
    household that changes its number afterwards must not rewrite history.
    """

    # 153, not 160. A message that will not fit is split into parts, and each
    # part spends 7 characters on the "(1/2)" marker that makes it readable
    # when the network delivers them out of order.
    SINGLE_LIMIT = 160
    PART_LIMIT = 153

    class Kind(models.TextChoices):
        MANUAL = "manual", "Sent by facilitator"
        AUTOMATIC = "automatic", "Automatic alert"

    class Status(models.TextChoices):
        """
        Where a message has got to.

        SENT and DELIVERED are kept apart deliberately. The gateway accepting
        a message is not the same as a phone receiving it, and an alert log
        that blurs the two cannot answer the only question anyone ever asks
        it: "I never got a text - did you send one?"
        """

        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent to network"
        DELIVERED = "delivered", "Delivered"
        UNDELIVERED = "undelivered", "Not delivered"
        FAILED = "failed", "Failed"

    household = models.ForeignKey(
        Household, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sms_messages",
    )
    to_number = models.CharField(max_length=20)

    # Who this went to when it was not a household - the facilitator, or a
    # standing number. Stored as text rather than another foreign key: the log
    # has to survive that person's account being renamed or removed.
    to_name = models.CharField(max_length=120, blank=True)

    body = models.TextField()

    # The gateway's own id for this message, which is how its delivery state
    # is looked up later. Blank until the gateway has accepted it.
    provider_message_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_status = models.CharField(
        max_length=40, blank=True,
        help_text="The gateway's own word for it, kept verbatim for diagnosis.",
    )
    delivered_at = models.DateTimeField(null=True, blank=True)

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.MANUAL)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=200, blank=True)

    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="sent_sms",
    )
    notification = models.ForeignKey(
        "Notification", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sms_messages",
        help_text="The in-app notice this text accompanied, when there was one.",
    )
    event = models.ForeignKey(
        "LeakEvent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sms_messages",
    )

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.to_number} - {self.get_status_display()}"

    @property
    def recipient_label(self):
        if self.household is not None:
            return f"{self.household.household_id} ({self.household.owner_name})"
        if self.to_name:
            return f"{self.to_name} ({self.to_number})"
        return self.to_number

    @property
    def is_settled(self):
        """Nothing more will happen to this message on its own."""
        return self.status in {
            self.Status.DELIVERED, self.Status.UNDELIVERED, self.Status.FAILED
        }

    @classmethod
    def queue_to_number(cls, number, body, *, name="", kind=None, sent_by=None, event=None):
        """
        A message to somebody who is not a household - the facilitator, or a
        standing number for the barangay. Same queue, same log, same retries.
        """
        number = (number or "").strip()
        if not number:
            return []

        body = normalise_for_gsm(body)
        return [
            cls.objects.create(
                household=None,
                to_number=number,
                to_name=name,
                body=part,
                kind=kind or cls.Kind.AUTOMATIC,
                sent_by=sent_by,
                event=event,
            )
            for part in cls.split_body(body)
        ]

    @classmethod
    def queue(cls, household, body, *, kind=None, sent_by=None, notification=None, event=None):
        """
        Put a message in front of a household. Returns the rows created.

        Refuses silently when there is no number to send to: a household can
        be registered before anyone has collected a contact number, and that
        must not raise in the middle of sending a notice to twenty others.
        """
        number = (household.contact_number or "").strip()
        if not number:
            return []

        body = normalise_for_gsm(body)
        return [
            cls.objects.create(
                household=household,
                to_number=number,
                body=part,
                kind=kind or cls.Kind.MANUAL,
                sent_by=sent_by,
                notification=notification,
                event=event,
            )
            for part in cls.split_body(body)
        ]

    @classmethod
    def split_body(cls, body):
        """
        One message, or several numbered parts if it will not fit.

        A silently truncated alert is worse than a long one: the part that
        gets cut is the end, which is where the instruction usually is.
        """
        body = body.strip()
        if len(body) <= cls.SINGLE_LIMIT:
            return [body]

        chunks = [body[i:i + cls.PART_LIMIT] for i in range(0, len(body), cls.PART_LIMIT)]
        total = len(chunks)
        return [f"({n}/{total}) {chunk}" for n, chunk in enumerate(chunks, start=1)]


def normalise_for_gsm(text):
    """
    Fold text into characters a SIM800L can send in its default alphabet.

    A single smart quote pasted from Word switches the whole message to
    UCS-2, which cuts the limit from 160 characters to 70 - so a notice that
    looked fine in the browser silently arrives cut in half, or as three
    texts instead of one. Replacing the handful of characters that actually
    cause it is cheaper than explaining that to anyone.
    """
    replacements = {
        "\u2018": "'", "\u2019": "'",  # smart single quotes
        "\u201c": '"', "\u201d": '"',  # smart double quotes
        "\u2013": "-", "\u2014": "-", "\u2026": "...",  # en/em dash, ellipsis
        "\u00a0": " ",  # non-breaking space: invisible, and it costs a whole message
        "\u20b1": "PHP ", "\u00d7": "x", "\u2022": "-", "\u00b0": " deg",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


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

    # Kept in step with the newest message so the inbox can be ordered by
    # activity without a subquery per row.
    last_message_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"CMP-{timezone.now():%y%m%d}-{secrets.token_hex(2).upper()}"
        super().save(*args, **kwargs)

    @property
    def awaiting_reply(self):
        """True when the owner spoke last and nobody has answered."""
        last = self.messages.order_by("-created_at").first()
        return last is not None and last.from_owner

    def unread_for_facilitator(self):
        return self.messages.filter(from_owner=True, read_by_facilitator=False).count()

    def unread_for_owner(self):
        return self.messages.filter(from_owner=False, read_by_owner=False).count()


class ComplaintMessage(models.Model):
    """
    One message in a complaint thread.

    The complaint itself is the conversation: it keeps the subject, the
    category and the status, so a household with two problems still has two
    threads that can be resolved independently. Messages are the back and
    forth inside one of them.

    from_owner rather than comparing author to the household's user, because
    an owner's login can be deleted or re-issued while their messages stay.
    """

    complaint = models.ForeignKey(Complaint, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="complaint_messages",
    )
    from_owner = models.BooleanField(
        help_text="True if written by the household, False if by the facilitator."
    )
    body = models.TextField()
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    # One flag per side rather than a timestamp per reader: a thread only
    # ever has two parties, and this keeps the unread badges a simple count.
    read_by_facilitator = models.BooleanField(default=False)
    read_by_owner = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Touch the thread so the inbox can sort on it.
        Complaint.objects.filter(pk=self.complaint_id).update(last_message_at=self.created_at)

    def __str__(self):
        who = "owner" if self.from_owner else "facilitator"
        return f"{self.complaint.reference} - {who} at {self.created_at:%Y-%m-%d %H:%M}"

    def __str__(self):
        return f"{self.reference} - {self.subject}"


class PushSubscription(models.Model):
    """
    One browser's permission to put a notification on one phone's screen.

    Not one per household. A household is a family, and the portal is opened
    on whichever handset is nearest - the owner's phone, a son's phone, the
    phone that happens to be charging. Each of those is a separate
    subscription with its own keys, and all of them should buzz when that
    household's line starts leaking.

    The endpoint is a URL at the browser vendor's push service, and it is the
    identity: the same handset reinstalling the portal gets a new one, and the
    old one starts returning 410 Gone. That is the only reliable signal that a
    subscription is dead, so it is what prune() acts on - never a guess based
    on how long a row has been quiet.
    """

    household = models.ForeignKey(
        "Household", on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    # Long: FCM and Mozilla endpoints run past 200 characters and the spec
    # sets no ceiling. TextField rather than a CharField with a number in it
    # that some vendor will eventually exceed.
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)

    # Only to tell two handsets apart on the settings page; never parsed.
    user_agent = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    failures = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.household.household_id} - {self.endpoint[:40]}"

    @property
    def as_dict(self):
        """The shape pywebpush expects."""
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }


class WorkOrder(models.Model):
    """
    A piece of field work: a household visit, a repair, or maintenance.

    One model carries the whole life of that work, because in a barangay they
    are one process seen at different stages. Something needs attention - a
    household complains, a facilitator notices a leaking coupling, a line is
    flagged by its board. That becomes a REQUESTED order: the "reported
    anomaly" the administrator triages. Approving it assigns a facilitator and
    a date and moves it to SCHEDULED; rejecting it closes it with a reason.
    From there the assigned facilitator works it to DONE.

    Keeping anomaly, repair and visit in one table means the administrator
    sees one queue of outstanding work rather than three that have to be
    reconciled by hand - and a complaint, a flagged line and a facilitator's
    own report all land in the same place to be dealt with the same way.
    """

    class Kind(models.TextChoices):
        VISIT = "visit", "Household visit"
        REPAIR = "repair", "Repair"
        MAINTENANCE = "maintenance", "Maintenance"

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"        # reported, awaiting triage
        SCHEDULED = "scheduled", "Scheduled"         # approved, assigned, dated
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    # Statuses that need nobody to do anything more. Used for the "open work"
    # queues and badges so a done or rejected order drops out of them.
    CLOSED_STATUSES = {Status.DONE, Status.REJECTED, Status.CANCELLED}

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.VISIT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)

    barangay = models.ForeignKey(
        "Barangay", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="work_orders",
    )
    # What the work is about. A visit names a household; a repair or
    # maintenance usually names a hose line. Both are optional and SET_NULL so
    # deleting the household or line does not erase the record that work was
    # done - the title and barangay still say what it was.
    household = models.ForeignKey(
        "Household", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="work_orders",
    )
    segment = models.ForeignKey(
        "Segment", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="work_orders",
    )

    title = models.CharField(max_length=200)
    details = models.TextField(blank=True)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assignments",
        help_text="The facilitator who will carry out this work.",
    )
    scheduled_for = models.DateField(
        null=True, blank=True, help_text="The day the visit or repair is planned for.",
    )

    # Where it came from, so an approved order remembers what raised it.
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reported_work_orders",
        help_text="The facilitator who reported this, when it did not come from the admin.",
    )
    source_complaint = models.ForeignKey(
        "Complaint", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="work_orders",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_work_orders",
    )

    # Triage and completion trail.
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_work_orders",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=300, blank=True)
    resolution_note = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_open(self):
        return self.status not in self.CLOSED_STATUSES

    @property
    def is_requested(self):
        return self.status == self.Status.REQUESTED

    @property
    def target_household(self):
        """The household this work ultimately concerns, directly or via a line."""
        if self.household is not None:
            return self.household
        if self.segment is not None:
            return getattr(self.segment, "household", None)
        return None

    @property
    def where_label(self):
        household = self.target_household
        if household is not None:
            return f"{household.household_id} ({household.owner_name})"
        if self.segment is not None:
            return self.segment.name
        if self.barangay is not None:
            return self.barangay.name
        return "-"

    def __str__(self):
        return f"{self.get_kind_display()}: {self.title}"


class Announcement(models.Model):
    """
    An administrator's message to households, across one or every barangay.

    The facilitator's notice tool (NotificationForm) only ever reaches the one
    barangay they run, so it cannot say "every barangay" or "just Barangay X".
    The administrator can, which is the whole reason this is separate.

    The announcement is recorded here for the history and the counts; delivery
    reuses what households already read - a Notification row per household (so
    it appears in their Notices feed with no change on the owner side) and,
    optionally, an SMS through the same queue as everything else.
    """

    class Audience(models.TextChoices):
        ALL = "all", "All active households"
        BARANGAY = "barangay", "One barangay"
        SELECTED = "selected", "Selected households"

    title = models.CharField(max_length=150)
    body = models.TextField()
    audience = models.CharField(max_length=20, choices=Audience.choices, default=Audience.ALL)
    barangay = models.ForeignKey(
        "Barangay", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="announcements",
    )
    in_app = models.BooleanField(default=True)
    by_sms = models.BooleanField(default=False)
    by_email = models.BooleanField(default=False)

    # Recorded at send time so the history shows reach without recounting
    # against households that may since have been added or removed.
    recipients_count = models.PositiveIntegerField(default=0)
    sms_count = models.PositiveIntegerField(default=0)
    email_count = models.PositiveIntegerField(default=0)

    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="announcements",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def audience_label(self):
        if self.audience == self.Audience.BARANGAY and self.barangay is not None:
            return f"Brgy. {self.barangay.name}"
        return self.get_audience_display()

    def __str__(self):
        return self.title


class AuditLog(models.Model):
    """
    An append-only record of significant administrative actions.

    Who did what, and when. Deliberately write-once - there is no edit path
    anywhere in the app - because a log you can change is not evidence of
    anything. The actor's name is copied in as text as well as kept as a
    foreign key, so the record still reads after that account is deleted.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_entries",
    )
    actor_name = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=60, db_index=True)
    summary = models.CharField(max_length=255)
    target = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def record(cls, actor, action, summary, target=""):
        """Write one entry. `actor` may be a user or None (a system action)."""
        name = ""
        if actor is not None and getattr(actor, "is_authenticated", False):
            name = actor.get_full_name() or actor.username
        return cls.objects.create(
            actor=actor if (actor is not None and getattr(actor, "is_authenticated", False)) else None,
            actor_name=name,
            action=action,
            summary=summary[:255],
            target=target[:255],
        )

    def __str__(self):
        return f"{self.action}: {self.summary}"


class SystemSetting(models.Model):
    """
    System-wide switches, kept as a single row so they can be changed from the
    console instead of by editing a file and restarting.

    Just maintenance mode for now: a hold that shows households and
    facilitators a short notice and keeps them out, while leaving the admin
    console and the device ingest endpoint working so the person turning it on
    can still work and the boards never stop recording.
    """

    maintenance_mode = models.BooleanField(default=False)
    maintenance_message = models.CharField(
        max_length=255, blank=True,
        default="The system is briefly down for maintenance. Please check back shortly.",
    )

    # Leak/flow detection thresholds. The board decides against these locally
    # (so it keeps working when the WiFi drops), but the figures belong to the
    # site: they are sent to every board on the ingest reply
    # (core.api._device_config), so a change here reaches the hardware within
    # seconds, with no re-flashing.
    flow_warn_minutes = models.PositiveSmallIntegerField(
        default=30, validators=[MinValueValidator(1), MaxValueValidator(1440)],
        verbose_name="Water-running warning after (minutes)",
        help_text="Continuous flow for this many minutes raises a 'water running' warning.",
    )
    flow_leak_minutes = models.PositiveSmallIntegerField(
        default=60, validators=[MinValueValidator(1), MaxValueValidator(1440)],
        verbose_name="Treat as leak after (minutes)",
        help_text="Continuous flow for this many minutes is treated as a leak.",
    )
    valve_test_hour = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(0), MaxValueValidator(23)],
        verbose_name="Nightly test hour",
        help_text="The board closes the valve at this hour for its sealed-line pressure test.",
    )
    valve_test_grace_minutes = models.PositiveSmallIntegerField(
        default=60, validators=[MinValueValidator(1), MaxValueValidator(240)],
        verbose_name="Test grace period (minutes)",
        help_text="How long the valve stays closed while the board watches for a pressure drop.",
    )
    valve_test_drop_bar = models.FloatField(
        default=0.15, validators=[MinValueValidator(0.0), MaxValueValidator(12.0)],
        verbose_name="Leak if pressure drops more than (bar)",
        help_text="A pressure drop larger than this during the sealed-line test is treated as a leak.",
    )
    auto_close_on_flow_leak = models.BooleanField(
        default=True,
        verbose_name="Auto-close valve on a continuous-flow leak",
        help_text="Close the valve automatically when continuous flow has run long enough to count as a leak.",
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1  # one row, always
        super().save(*args, **kwargs)

    def __str__(self):
        return "System settings"


class ErrorLog(models.Model):
    """
    One row per unhandled server error, written by middleware.

    So a 500 leaves a trace the administrator can see in the console without
    reading the terminal, and without a log line scrolling away unseen during
    a demo. The message is the exception text, never a full traceback shown to
    a user - that stays in the server log where it belongs.
    """

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    path = models.CharField(max_length=255, blank=True)
    method = models.CharField(max_length=8, blank=True)
    status = models.PositiveSmallIntegerField(default=500)
    exception_type = models.CharField(max_length=120, blank=True)
    message = models.TextField(blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.exception_type} at {self.path}"


class LoginCode(models.Model):
    """
    A one-time sign-in code, texted or emailed after the password is right.

    Ten minutes to use it, five guesses, one use. Stored as typed rather than
    hashed on purpose: it is short-lived, single-use and attempt-limited, and
    in development it has to be readable so a demo can sign in with no SMS
    credit and no mailbox - the login page shows it when DEBUG is on. A code
    somebody could brute-force in five tries would not be made safer by
    hashing; the limit is the protection.
    """

    CODE_LENGTH = 6
    MAX_ATTEMPTS = 5

    class Channel(models.TextChoices):
        SMS = "sms", "Text message"
        EMAIL = "email", "Email"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="login_codes",
    )
    code = models.CharField(max_length=12)
    channel = models.CharField(max_length=8, choices=Channel.choices)
    # Where it went, as text, so the page can say "sent to +63917...501"
    # without a second lookup and the record survives the account changing.
    sent_to = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def generate(cls):
        return f"{secrets.randbelow(10 ** cls.CODE_LENGTH):0{cls.CODE_LENGTH}d}"

    @classmethod
    def issue(cls, user, channel, sent_to=""):
        """
        A fresh code for this user, replacing any earlier unused one.

        One live code at a time: two would let an old text be used after a
        newer one was requested, which makes "resend" a way to extend a code
        rather than replace it.
        """
        cls.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
        return cls.objects.create(
            user=user,
            code=cls.generate(),
            channel=channel,
            sent_to=sent_to[:120],
            expires_at=timezone.now() + timezone.timedelta(minutes=settings.OTP_VALIDITY_MINUTES),
        )

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_usable(self):
        return self.used_at is None and not self.is_expired and self.attempts < self.MAX_ATTEMPTS

    def masked_destination(self):
        """'+63917***501' or 'ma***@gmail.com' - enough to recognise, not to copy."""
        value = self.sent_to or ""
        if "@" in value:
            name, _, domain = value.partition("@")
            return f"{name[:2]}***@{domain}"
        digits = value
        return f"{digits[:6]}***{digits[-3:]}" if len(digits) > 9 else "your number"

    def __str__(self):
        return f"{self.user} via {self.channel} ({'used' if self.used_at else 'open'})"
