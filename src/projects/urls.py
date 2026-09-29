from django.urls import path

from . import api, views

app_name = "projects"

urlpatterns = [
    path("projects/", views.gallery, name="gallery"),
    path("projects/<int:pk>/", views.project_detail, name="detail"),
    path("projects/<int:pk>/comments/", views.comment_add, name="comment_add"),
    path("projects/<int:pk>/comments/<int:comment_id>/remove/", views.comment_remove, name="comment_remove"),
    path("events/<slug:slug>/projects/", views.gallery, name="event_gallery"),
    path("events/<slug:slug>/submission/", views.submission_edit, name="edit"),
    path("events/<slug:slug>/submission/withdraw/", views.submission_withdraw, name="withdraw"),
    path("events/<slug:slug>/submission/images/", views.image_upload, name="image_upload"),
    path("events/<slug:slug>/submission/images/<int:image_id>/delete/", views.image_delete,
         name="image_delete"),
    # JSON. No trailing slashes, so a POST never meets an APPEND_SLASH redirect.
    path("api/projects", api.project_list, name="api_projects"),
    path("api/events/<slug:slug>/submission", api.submission, name="api_submission"),
]
