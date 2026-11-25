from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("schedule/", views.schedule, name="schedule"),
    path("bookings/", views.booking_list, name="booking_list"),
    path("bookings/new/", views.booking_create, name="booking_create"),
    path("bookings/export.csv", views.export_bookings, name="export_bookings"),
    path("bookings/<int:pk>/", views.booking_detail, name="booking_detail"),
    path("bookings/<int:pk>/status/", views.booking_status, name="booking_status"),
    path("bookings/<int:pk>/refund-handled/", views.booking_refund_handled, name="booking_refund_handled"),
    path("bookings/<int:pk>/pay-credit/", views.booking_pay_credit, name="booking_pay_credit"),
    path("bookings/<int:pk>/refund-credit/", views.booking_refund_credit, name="booking_refund_credit"),
    path("series/<int:pk>/cancel/", views.series_cancel, name="series_cancel"),
    path("credit/", views.credit_list, name="credit_list"),
    path("credit/<int:pk>/", views.credit_detail, name="credit_detail"),
    path("credit/<int:pk>/adjust/", views.credit_adjust, name="credit_adjust"),
    path("cash/", views.cash_report, name="cash_report"),
    path("cash/<int:pk>/reopen/", views.cash_reopen, name="cash_reopen"),
    path("invoices/", views.invoice_list, name="invoice_list"),
    path("invoices/new/", views.invoice_create, name="invoice_create"),
    path("invoices/export.csv", views.export_invoices, name="export_invoices"),
    path("invoices/<int:pk>/", views.invoice_detail, name="invoice_detail"),
    path("invoices/<int:pk>/payment/", views.invoice_payment, name="invoice_payment"),
    path("invoices/<int:pk>/void/", views.invoice_void, name="invoice_void"),
    path("customers/", views.customers, name="customers"),
    path("settings/", views.settings_view, name="settings"),
    path("team/", views.team, name="team"),
    path("team/<int:pk>/remove/", views.team_remove, name="team_remove"),
    path("subscription/", views.subscription, name="subscription"),
    path("<str:kind>/", views.catalog_list, name="catalog_list"),
    path("<str:kind>/new/", views.catalog_form, name="catalog_create"),
    path("<str:kind>/<int:pk>/", views.catalog_form, name="catalog_edit"),
]
