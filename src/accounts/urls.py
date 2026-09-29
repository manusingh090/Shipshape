from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("login/demo/", views.demo_login, name="demo_login"),
    path("logout/", views.logout_view, name="logout"),
    path("signup/", views.signup_view, name="signup"),
    path("account/", views.account_view, name="account"),
    path("account/password/", views.password_view, name="password"),
    path("account/sessions/<int:pk>/end/", views.end_session_view, name="end_session"),
    path("account/sessions/end-others/", views.end_other_sessions_view, name="end_other_sessions"),
    path("account/tokens/", views.tokens_view, name="tokens"),
    path("admin/", views.admin_home, name="admin"),
    path("admin/users/<int:pk>/", views.admin_user_update, name="admin_user_update"),
    path("admin/audit/", views.admin_audit, name="admin_audit"),
]
