from django.contrib import admin
from django.urls import include, path
from django.views.i18n import JavaScriptCatalog

from core.language import set_language

urlpatterns = [
    path("admin/", admin.site.urls),
    # The EN/FIL toggle. Django's own view stores the choice in a cookie and
    # redirects back; ours also writes an owner's choice to their household
    # so the texts they receive follow the screen. Declared before the
    # include so the name "set_language" resolves here.
    path("i18n/setlang/", set_language, name="set_language"),
    path("i18n/", include("django.conf.urls.i18n")),
    # The translations app.js needs, served in the active language. Loaded
    # by base.html before app.js, which then calls gettext() like a template.
    path("jsi18n/", JavaScriptCatalog.as_view(packages=["core"]), name="javascript-catalog"),
    path("", include("core.urls")),
]

admin.site.site_header = "Smart Hose Leak Detection System"
admin.site.site_title = "Smart Hose Leak Admin"
admin.site.index_title = "System Administration"
