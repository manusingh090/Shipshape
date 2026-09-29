from django.urls import include, path

from events import views as event_views
from projects import views as project_views
from records import views as record_views
from webhooks import views as webhook_views

from . import views

urlpatterns = [
    path("", event_views.home, name="home"),
    path("healthz", views.healthz, name="healthz"),
    path(".well-known/shipshape-records.json", record_views.well_known, name="records_key"),
    path("media/<path:path>", project_views.media, name="media"),
    path("", include("accounts.urls")),
    path("", include("events.urls")),
    path("", include("teams.urls")),
    path("", include("projects.urls")),
    path("", include("judging.urls")),
    path("", include("voting.urls")),
    path("", include("integrity.urls")),
    path("", include("api.urls")),
    path("", include("webhooks.urls")),
    path("", include("records.urls")),
    path("", include("embeds.urls")),
    path("", include("transfer.urls")),
    path("webhooks/<int:pk>/receive", webhook_views.receiver, name="webhook_receiver"),
]

handler400 = "portal.views.bad_request"
handler403 = "portal.views.forbidden"
handler404 = "portal.views.not_found"
handler500 = "portal.views.server_error"
