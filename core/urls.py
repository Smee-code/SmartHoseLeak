from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import TemplateView

from . import api, views

urlpatterns = [
    # Public
    path("", views.home, name="home"),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("post-login/", views.post_login, name="post_login"),
    path(
        "password-change/",
        auth_views.PasswordChangeView.as_view(
            template_name="registration/password_change.html", success_url="/post-login/"
        ),
        name="password_change",
    ),

    # Facilitator dashboard
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/pressure/", views.pressure_monitor, name="pressure_monitor"),
    path("dashboard/households/", views.household_list, name="household_list"),
    path("dashboard/households/new/", views.household_form, name="household_create"),
    path("dashboard/households/<int:pk>/", views.household_detail, name="household_detail"),
    path("dashboard/households/<int:pk>/edit/", views.household_form, name="household_edit"),
    path("dashboard/households/<int:pk>/delete/", views.household_delete, name="household_delete"),
    path(
        "dashboard/households/<int:pk>/reset-password/",
        views.household_reset_password,
        name="household_reset_password",
    ),
    path("dashboard/lines/", views.segment_list, name="segment_list"),
    path("dashboard/lines/new/", views.segment_create, name="segment_create"),
    path("dashboard/lines/<int:pk>/edit/", views.segment_edit, name="segment_edit"),
    path("dashboard/lines/<int:pk>/delete/", views.segment_delete, name="segment_delete"),
    path("dashboard/lines/<int:pk>/maintenance/", views.segment_maintenance, name="segment_maintenance"),
    path("dashboard/lines/<int:pk>/valve-test/", views.segment_valve_test, name="segment_valve_test"),
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

    # Owner portal
    path("portal/", views.owner_home, name="owner_home"),
    path("portal/notifications/", views.owner_notifications, name="owner_notifications"),
    path("portal/complaints/", views.owner_complaints, name="owner_complaints"),
    path("portal/complaints/<int:pk>/", views.owner_complaints, name="owner_complaint_thread"),
    path("portal/pressure/", views.owner_pressure, name="owner_pressure"),

    # JSON API
    path("api/ingest/", api.ingest, name="api_ingest"),
    path("api/live/", api.live, name="api_live"),
    path("api/history/", api.history, name="api_history"),

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
