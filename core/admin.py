from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    Announcement,
    AuditLog,
    Barangay,
    SmsMessage,
    Complaint,
    Device,
    ErrorLog,
    Household,
    LatestReading,
    LeakEvent,
    Notification,
    ReadingRollup,
    Segment,
    SystemSetting,
    User,
    WorkOrder,
)


@admin.register(Barangay)
class BarangayAdmin(admin.ModelAdmin):
    list_display = ("name", "municipality", "province", "is_active", "created_at")
    list_filter = ("is_active", "municipality", "province")
    search_fields = ("name",)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "get_full_name", "role", "barangay", "contact_number", "is_active", "last_login")
    list_filter = ("role", "barangay", "is_active", "is_staff")
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Smart Hose Leak", {"fields": ("role", "barangay", "contact_number", "must_change_password")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("Smart Hose Leak", {"fields": ("role", "barangay", "contact_number")}),
    )


class SegmentInline(admin.TabularInline):
    model = Segment
    extra = 0
    fields = ("module_index", "name", "status", "is_active")


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("device_id", "label", "is_online", "last_seen", "last_ip")
    readonly_fields = ("api_key", "last_seen", "last_ip")
    inlines = [SegmentInline]

    @admin.display(boolean=True, description="Online")
    def is_online(self, obj):
        return obj.is_online


@admin.register(Segment)
class SegmentAdmin(admin.ModelAdmin):
    """One hose line per household, edited here or from Dashboard -> Lines."""

    list_display = ("name", "device", "low_pressure_bar", "status", "is_active")
    list_filter = ("status", "is_active", "device")
    list_editable = ("low_pressure_bar",)
    search_fields = ("name",)
    fieldsets = (
        (None, {"fields": ("device", "module_index", "name", "is_active")}),
        (
            "Detection",
            {
                "fields": ("low_pressure_bar",),
                "description": "Sent to the device on its next upload, so changes here take "
                "effect within seconds without re-flashing the board.",
            },
        ),
        ("Map coordinates (optional)", {"fields": (("start_lat", "start_lng"), ("end_lat", "end_lng"))}),
        ("Status", {"fields": ("status", "status_changed_at", "maintenance_note")}),
    )


@admin.register(LatestReading)
class LatestReadingAdmin(admin.ModelAdmin):
    list_display = ("segment", "pressure_bar", "pressure_psi", "flow_lpm", "device_status", "updated_at")
    readonly_fields = [f.name for f in LatestReading._meta.fields]


@admin.register(ReadingRollup)
class ReadingRollupAdmin(admin.ModelAdmin):
    list_display = ("minute", "segment", "avg_pressure_bar", "avg_flow_lpm", "sample_count", "leak_samples")
    list_filter = ("segment",)
    date_hierarchy = "minute"


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = ("household_id", "owner_name", "barangay", "contact_number", "segment", "account_status", "user")
    list_filter = ("account_status", "barangay", "segment")
    search_fields = ("household_id", "owner_name", "address", "contact_number")


@admin.register(LeakEvent)
class LeakEventAdmin(admin.ModelAdmin):
    list_display = ("detected_at", "segment", "status", "severity", "device_severity", "trigger", "cleared_at")
    list_filter = ("status", "severity", "segment")
    date_hierarchy = "detected_at"


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "title", "kind", "recipient", "read_at")
    list_filter = ("kind", "created_at")
    search_fields = ("title", "body")


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    list_display = ("reference", "household", "category", "subject", "status", "created_at")
    list_filter = ("status", "category")
    search_fields = ("reference", "subject", "body")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """The administrative trail. Read-only, like the SMS log: evidence you can
    edit is not evidence."""

    list_display = ("created_at", "actor_name", "action", "summary", "target")
    list_filter = ("action",)
    search_fields = ("actor_name", "summary", "target")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ErrorLog)
class ErrorLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "exception_type", "method", "path", "user")
    list_filter = ("exception_type",)
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in ErrorLog._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ("maintenance_mode", "updated_at", "updated_by")


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "audience", "barangay", "in_app", "by_sms", "recipients_count", "created_at")
    list_filter = ("audience", "in_app", "by_sms")
    search_fields = ("title", "body")
    date_hierarchy = "created_at"


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "status", "barangay", "assigned_to", "scheduled_for", "created_at")
    list_filter = ("kind", "status", "barangay")
    search_fields = ("title", "details")
    date_hierarchy = "created_at"
    autocomplete_fields = ()


@admin.register(SmsMessage)
class SmsMessageAdmin(admin.ModelAdmin):
    """
    The delivery log. Read-only on purpose: it records what was actually
    sent, and a record you can edit is not evidence of anything.
    """

    list_display = ("created_at", "to_number", "household", "kind", "status", "attempts", "sent_at")
    list_filter = ("status", "kind", "created_at")
    search_fields = ("to_number", "body", "household__household_id", "household__owner_name")
    readonly_fields = ("created_at", "sent_at", "attempts", "error")
