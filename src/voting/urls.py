from django.urls import path

from . import api, views

app_name = "voting"

urlpatterns = [
    path("events/<slug:slug>/vote/", views.ballot, name="ballot"),
    path("events/<slug:slug>/vote/link/<str:token>/", views.ballot_link, name="ballot_link"),
    path("events/<slug:slug>/vote/confirm/<str:token>/", views.email_confirm, name="email_confirm"),
    path("events/<slug:slug>/vote/results/", views.results, name="results"),
    path("events/<slug:slug>/manage/voting/", views.manage, name="manage"),
    # JSON. No trailing slash, as with the rest of the API.
    path("api/events/<slug:slug>/ballot", api.ballot, name="api_ballot"),
    path("api/events/<slug:slug>/vote/results", api.results, name="api_results"),
]
