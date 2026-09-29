from django.urls import path

from . import views

app_name = "webhooks"

urlpatterns = [
    path("events/<slug:slug>/manage/webhooks/", views.manage, name="manage"),
    path("admin/webhooks/", views.admin_manage, name="admin"),
]
