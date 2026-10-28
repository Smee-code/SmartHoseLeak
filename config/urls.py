from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Django's set_language view, used by the EN/FIL toggle to store the
    # chosen language in a cookie and redirect back.
    path("i18n/", include("django.conf.urls.i18n")),
    path("", include("core.urls")),
]

admin.site.site_header = "Smart Hose Leak Detection System"
admin.site.site_title = "Smart Hose Leak Admin"
admin.site.index_title = "System Administration"
