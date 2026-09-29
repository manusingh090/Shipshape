from django.urls import path

from . import api, judge_views, views

app_name = "judging"

urlpatterns = [
    # Judges
    path("judge/", judge_views.home, name="home"),
    path("judge/<slug:slug>/", judge_views.queue, name="queue"),
    path("judge/<slug:slug>/projects/<int:pk>/", judge_views.score, name="score"),
    path("judge/<slug:slug>/projects/<int:pk>/conflict/", judge_views.conflict, name="conflict"),
    path("judge-invites/<str:token>/", judge_views.invite, name="invite"),
    # Organizer console
    path("events/<slug:slug>/manage/judging/", views.progress, name="progress"),
    path("events/<slug:slug>/manage/judging/rubric/", views.rubric, name="rubric"),
    path("events/<slug:slug>/manage/judging/judges/", views.judges, name="judges"),
    path("events/<slug:slug>/manage/judging/assignments/", views.assignments, name="assignments"),
    path("events/<slug:slug>/manage/judging/results/", views.results, name="results"),
    # Its own console tab: the files cover every stage, not only judging.
    path("events/<slug:slug>/manage/exports/", views.exports, name="exports"),
    path("events/<slug:slug>/results/", views.public_results, name="public_results"),
    # JSON and CSV. No trailing slashes, as with the rest of the API.
    path("api/judge/scores", api.judge_scores, name="api_judge_scores"),
    path("api/judge/assignments", api.judge_assignments, name="api_judge_assignments"),
    path("api/events/<slug:slug>/judging/scores", api.submit_score, name="api_submit_score"),
    path("api/events/<slug:slug>/judging/progress", api.progress, name="api_progress"),
    path("api/events/<slug:slug>/judging/results", api.results, name="api_results"),
    path("api/events/<slug:slug>/export/<str:stage>.csv", api.export, name="export"),
]
