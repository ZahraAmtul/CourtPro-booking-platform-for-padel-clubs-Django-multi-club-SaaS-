from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.EmailLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("signup/", views.signup, name="signup"),
    path("my-bookings/", views.my_bookings, name="my_bookings"),
    path("profile/", views.profile, name="profile"),
    path("magic/<str:token>/", views.magic_login, name="magic_login"),
]
