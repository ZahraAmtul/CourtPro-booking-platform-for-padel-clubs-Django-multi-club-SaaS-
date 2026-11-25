from django.urls import path

from . import views

app_name = "bookings"

urlpatterns = [
    path("book/", views.book, name="book"),
    path("book/details/", views.details, name="details"),
    path("manage-booking/", views.manage, name="manage"),
    path("booking/<str:reference>/", views.detail, name="detail"),
    path("booking/<str:reference>/cancel/", views.cancel, name="cancel"),
]
