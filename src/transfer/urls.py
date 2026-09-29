from django.urls import path

from . import views

app_name = "transfer"

urlpatterns = [
    path("organize/import/", views.import_event, name="import"),
    path("events/<slug:slug>/manage/transfer/", views.manage, name="manage"),
    path("events/<slug:slug>/manage/transfer/archive.zip", views.download_archive, name="archive"),
    path("events/<slug:slug>/manage/transfer/fixtures.json", views.download_fixture, name="fixture"),
]
