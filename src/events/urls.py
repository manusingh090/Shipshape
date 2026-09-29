from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("organize/", views.organize, name="organize"),
    path("events/new/", views.event_create, name="create"),
    path("events/<slug:slug>/", views.event_detail, name="detail"),
    path("events/<slug:slug>/manage/", views.manage, name="manage"),
    path("events/<slug:slug>/manage/details/", views.manage_details, name="manage_details"),
    path("events/<slug:slug>/manage/tracks/", views.manage_tracks, name="manage_tracks"),
    path("events/<slug:slug>/manage/prizes/", views.manage_prizes, name="manage_prizes"),
    path("events/<slug:slug>/manage/questions/", views.manage_questions, name="manage_questions"),
    path("events/<slug:slug>/manage/people/", views.manage_people, name="manage_people"),
    path("events/<slug:slug>/manage/people/<int:pk>/remove/", views.manage_people_remove,
         name="manage_people_remove"),
    path("events/<slug:slug>/manage/submissions/", views.manage_submissions, name="manage_submissions"),
    path("events/<slug:slug>/manage/submissions/<int:pk>/promote/", views.promote_duplicate,
         name="promote_duplicate"),
    path("events/<slug:slug>/manage/teams/", views.manage_teams, name="manage_teams"),
    path("events/<slug:slug>/manage/activity/", views.manage_activity, name="manage_activity"),
]
