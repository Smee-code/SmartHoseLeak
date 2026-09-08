from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    Complaint,
    Device,
    Household,
    LatestReading,
    LeakEvent,
    Notification,
    ReadingRollup,
    Segment,
    User,
)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "get_full_name", "role", "contact_number", "is_active", "last_login")
    list_filter = ("role", "is_active", "is_staff")
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Smart Hose Leak", {"fields": ("role", "contact_number", "must_change_password")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("Smart Hose Leak", {"fields": ("role", "contact_number")}),
    )


class SegmentInline(admin.TabularInline):
    model = Segment
    extra = 0
    fields = ("module_index", "name", "start_m", "end_m", "status", "is_active")


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
    """The distance markers the paper relies on are edited here."""

    list_display = ("module_index", "name", "device", "start_m", "end_m", "length_m", "status", "is_active")
    list_filter = ("status", "is_active", "device")
    list_editable = ("start_m", "end_m")
    search_fields = ("name",)
    fieldsets = (
        (None, {"fields": ("device", "module_index", "name", "is_active")}),
        (
            "Distance markers",
            {
                "fields": ("start_m", "end_m"),
                "description": "Metres from the main valve. A leak on this module is reported "
                "as lying between these two markers.",
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
    list_display = ("household_id", "owner_name", "contact_number", "segment", "account_status", "user")
    list_filter = ("account_status", "segment")
    search_fields = ("household_id", "owner_name", "address", "contact_number")


@admin.register(LeakEvent)
class LeakEventAdmin(admin.ModelAdmin):
    list_display = ("detected_at", "segment", "status", "severity", "location_label", "cleared_at")
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
