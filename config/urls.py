from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
]

admin.site.site_header = "Smart Hose Leak Detection System"
admin.site.site_title = "Smart Hose Leak Admin"
admin.site.index_title = "System Administration"
