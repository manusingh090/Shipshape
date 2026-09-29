from django.urls import path

from . import views

app_name = "integrity"

urlpatterns = [
    path("events/<slug:slug>/manage/integrity/", views.review, name="review"),
]
