"""URLs for the platform host (the bare PLATFORM_DOMAIN): landing page, club signup, platform admin."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import path

from tenants import views

urlpatterns = [
    path("", views.landing, name="platform_landing"),
    path("start/", views.signup, name="platform_signup"),
    path("club-login/", views.club_login, name="platform_club_login"),
    path("login/", views.PlatformLoginView.as_view(), name="platform_login"),
    path("logout/", LogoutView.as_view(next_page="platform_landing"), name="platform_logout"),
    path("platform/", views.platform_admin, name="platform_admin"),
    path("platform/clubs/<int:pk>/", views.platform_club, name="platform_club"),
    path("platform/clubs/<int:pk>/action/", views.platform_club_action, name="platform_club_action"),
    path("admin/", admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
