from django.urls import path

from . import views

app_name = "teams"

urlpatterns = [
    path("events/<slug:slug>/team/", views.my_team, name="mine"),
    path("events/<slug:slug>/team/rename/", views.rename, name="rename"),
    path("events/<slug:slug>/team/invite/reset/", views.reset_invite, name="reset_invite"),
    path("events/<slug:slug>/team/leave/", views.leave, name="leave"),
    path("events/<slug:slug>/team/remove/<int:user_id>/", views.remove_member, name="remove_member"),
    path("join/<str:code>/", views.join, name="join"),
]
