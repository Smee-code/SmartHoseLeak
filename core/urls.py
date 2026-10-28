from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import TemplateView

from . import api, views

urlpatterns = [
    # Public
    path("", views.home, name="home"),
    # Sign in is a dialog over the landing page; this view also serves the
    # standalone /login/ fallback and re-renders the landing on a bad password.
    path("login/", views.LandingLoginView.as_view(), name="login"),
    # The second step: the one-time code sent after the password is right.
    path("login/code/", views.login_code, name="login_code"),
    path("login/code/resend/", views.login_code_resend, name="login_code_resend"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("post-login/", views.post_login, name="post_login"),
    path(
        "password-change/",
        auth_views.PasswordChangeView.as_view(
            template_name="registration/password_change.html", success_url="/post-login/"
        ),
        name="password_change",
    ),

    # Forgot a password: email a one-time reset link. The link's address is
    # built from the request host, so opening this page through the public
    # tunnel produces a link that works away from this machine.
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.txt",
            subject_template_name="registration/password_reset_subject.txt",
            success_url="/password-reset/sent/",
        ),
        name="password_reset",
    ),
    path(
        "password-reset/sent/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
            success_url="/reset/done/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),

    # Facilitator dashboard
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/pressure/", views.pressure_monitor, name="pressure_monitor"),
    path("dashboard/households/", views.household_list, name="household_list"),
    path("dashboard/households/<int:pk>/", views.household_detail, name="household_detail"),
    path("dashboard/households/<int:pk>/edit/", views.household_form, name="household_edit"),
    path("dashboard/households/<int:pk>/delete/", views.household_delete, name="household_delete"),
    path("dashboard/households/<int:pk>/activate/", views.household_activate, name="household_activate"),
    path("dashboard/households/<int:pk>/register-board/", views.household_register_board, name="household_register_board"),
    path(
        "dashboard/households/<int:pk>/reset-password/",
        views.household_reset_password,
        name="household_reset_password",
    ),
    path(
        "dashboard/households/<int:pk>/send-credentials/",
        views.household_send_credentials,
        name="household_send_credentials",
    ),
    path(
        "dashboard/households/<int:pk>/refresh-route/",
        views.household_refresh_route,
        name="household_refresh_route",
    ),
    path("dashboard/lines/", views.segment_list, name="segment_list"),
    path("dashboard/lines/new/", views.segment_create, name="segment_create"),
    path("dashboard/lines/<int:pk>/edit/", views.segment_edit, name="segment_edit"),
    path("dashboard/lines/<int:pk>/delete/", views.segment_delete, name="segment_delete"),
    path("dashboard/lines/<int:pk>/maintenance/", views.segment_maintenance, name="segment_maintenance"),
    path("dashboard/lines/<int:pk>/valve-test/", views.segment_valve_test, name="segment_valve_test"),
    path("dashboard/devices/new/", views.device_create, name="device_create"),
    path("dashboard/devices/<int:pk>/new-key/", views.device_regenerate_key, name="device_regenerate_key"),
    path("dashboard/devices/<int:pk>/claim-code/", views.device_issue_claim_code, name="device_issue_claim_code"),
    path("dashboard/devices/<int:pk>/delete/", views.device_delete, name="device_delete"),
    path("dashboard/events/", views.event_list, name="event_list"),
    path("dashboard/events/<int:pk>/acknowledge/", views.event_acknowledge, name="event_acknowledge"),
    path("dashboard/complaints/", views.complaint_list, name="complaint_list"),
    # The inbox and an open thread are the same view, so the list stays
    # beside the conversation the way a mail client works.
    path("dashboard/complaints/<int:pk>/", views.complaint_list, name="complaint_thread"),
    path("dashboard/complaints/<int:pk>/reply/", views.complaint_reply, name="complaint_reply"),
    path("dashboard/complaints/<int:pk>/status/", views.complaint_status, name="complaint_status"),
    path("dashboard/notifications/", views.notification_center, name="notification_center"),
    path("dashboard/sms/<int:pk>/retry/", views.sms_retry, name="sms_retry"),
    path("dashboard/reports/", views.reports, name="reports"),
    path("dashboard/export/<str:kind>.csv", views.export_csv, name="export_csv"),
    path("dashboard/assignments/", views.my_assignments, name="my_assignments"),
    path("dashboard/assignments/<int:pk>/update/", views.assignment_update, name="assignment_update"),

    # Admin console
    path("console/", views.console_dashboard, name="console_dashboard"),
    path("console/barangays/", views.barangay_list, name="barangay_list"),
    path("console/barangays/new/", views.barangay_form, name="barangay_create"),
    path("console/barangays/<int:pk>/edit/", views.barangay_form, name="barangay_edit"),
    path("console/barangays/<int:pk>/toggle/", views.barangay_toggle, name="barangay_toggle"),
    path("console/facilitators/", views.facilitator_list, name="facilitator_list"),
    path("console/facilitators/new/", views.facilitator_create, name="facilitator_create"),
    path("console/facilitators/<int:pk>/edit/", views.facilitator_edit, name="facilitator_edit"),
    path("console/facilitators/<int:pk>/delete/", views.facilitator_delete, name="facilitator_delete"),
    path("console/households/", views.admin_household_list, name="admin_household_list"),
    path("console/households/new/", views.admin_household_create, name="admin_household_create"),
    path("console/households/<int:pk>/edit/", views.admin_household_edit, name="admin_household_edit"),
    path("console/households/<int:pk>/delete/", views.admin_household_delete, name="admin_household_delete"),
    path("console/accounts/", views.account_list, name="account_list"),
    path("console/accounts/<int:pk>/edit/", views.account_edit, name="account_edit"),
    path("console/accounts/<int:pk>/suspend/", views.account_suspend, name="account_suspend"),
    path("console/accounts/<int:pk>/delete/", views.account_delete, name="account_delete"),
    # Field operations (admin side)
    path("console/work/", views.work_order_list, name="work_order_list"),
    path("console/work/new/", views.work_order_create, name="work_order_create"),
    path("console/work/<int:pk>/", views.work_order_detail, name="work_order_detail"),
    path("console/work/<int:pk>/edit/", views.work_order_edit, name="work_order_edit"),
    path("console/work/<int:pk>/approve/", views.work_order_approve, name="work_order_approve"),
    path("console/work/<int:pk>/reject/", views.work_order_reject, name="work_order_reject"),
    path("console/work/<int:pk>/status/", views.work_order_status, name="work_order_status"),
    # Communications & analytics
    path("console/pressure/", views.admin_pressure, name="admin_pressure"),
    path("console/analytics/", views.analytics, name="analytics"),
    path("console/announcements/", views.announcement_list, name="announcement_list"),
    path("console/announcements/new/", views.announcement_create, name="announcement_create"),
    # Data & system operations
    path("console/data/", views.data_tools, name="data_tools"),
    path("console/data/export/<str:kind>.csv", views.export_console, name="export_console"),
    path("console/data/import/", views.household_import, name="household_import"),
    path("console/data/backup/", views.backup_download, name="backup_download"),
    path("console/data/restore/", views.restore_upload, name="restore_upload"),
    path("console/audit/", views.audit_log, name="audit_log"),
    path("console/system/", views.system_status, name="system_status"),
    path("console/system/maintenance/", views.maintenance_toggle, name="maintenance_toggle"),
    path("console/system/detection/", views.detection_settings, name="detection_settings"),
    # Archive
    path("console/archive/", views.archive_list, name="archive_list"),
    path("console/archive/households/<int:pk>/restore/", views.archive_restore_household, name="archive_restore_household"),
    path("console/archive/facilitators/<int:pk>/restore/", views.archive_restore_facilitator, name="archive_restore_facilitator"),

    # Owner portal
    path("portal/", views.owner_home, name="owner_home"),
    path("portal/notifications/", views.owner_notifications, name="owner_notifications"),
    path("portal/complaints/", views.owner_complaints, name="owner_complaints"),
    path("portal/complaints/<int:pk>/", views.owner_complaints, name="owner_complaint_thread"),
    path("portal/pressure/", views.owner_pressure, name="owner_pressure"),
    path("portal/settings/", views.owner_settings, name="owner_settings"),
    path("portal/device/status/", views.owner_device_status, name="owner_device_status"),
    path("portal/device/new-code/", views.owner_device_reissue, name="owner_device_reissue"),
    path("portal/device/resend/", views.owner_device_resend, name="owner_device_resend"),

    # JSON API
    path("api/ingest/", api.ingest, name="api_ingest"),
    path("api/claim/", api.claim, name="api_claim"),
    path("api/live/", api.live, name="api_live"),
    path("api/history/", api.history, name="api_history"),
    path("api/push/subscribe/", api.push_subscribe, name="api_push_subscribe"),
    path("api/push/unsubscribe/", api.push_unsubscribe, name="api_push_unsubscribe"),
    path("api/push/test/", api.push_test, name="api_push_test"),

    # PWA
    path(
        "sw.js",
        TemplateView.as_view(template_name="sw.js", content_type="application/javascript"),
        name="service_worker",
    ),
    path(
        "manifest.webmanifest",
        TemplateView.as_view(template_name="manifest.webmanifest", content_type="application/manifest+json"),
        name="manifest",
    ),
    path("offline/", TemplateView.as_view(template_name="offline.html"), name="offline"),
]
