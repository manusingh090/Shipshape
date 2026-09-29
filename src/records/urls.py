from django.urls import path

from . import views

app_name = "records"

urlpatterns = [
    path("records/<str:code>/", views.detail, name="detail"),
    path("records/<str:code>.pdf", views.pdf, name="pdf"),
    path("records/<str:code>.json", views.signed_json, name="json"),
    path("records/<str:code>/scores/", views.scores, name="scores"),
    path("verify/", views.verify, name="verify"),
    path("verify/<str:code>/", views.verify_code, name="verify_code"),
    path("events/<slug:slug>/manage/certificates/", views.manage, name="manage"),
]
