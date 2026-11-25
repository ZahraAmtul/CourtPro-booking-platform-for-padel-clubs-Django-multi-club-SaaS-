from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("invoice/<uuid:token>/", views.public_invoice, name="public_invoice"),
    path("pay/<uuid:token>/", views.pay, name="pay"),
    path("payments/return/<str:code>/", views.gateway_return, name="gateway_return"),
    path("payments/jazzcash/ipn/", views.jazzcash_ipn, name="jazzcash_ipn"),
]
