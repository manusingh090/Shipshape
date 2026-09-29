from django.urls import path

from . import views

app_name = "embeds"

urlpatterns = [
    path("embed.js", views.loader, name="loader"),
    path("embed/events/<slug:slug>/gallery", views.widget, name="widget"),
    path("embed/events/<slug:slug>/gallery.json", views.feed, name="feed"),
    path("events/<slug:slug>/manage/embed/", views.manage, name="manage"),
]
