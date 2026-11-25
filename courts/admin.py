from django.contrib import admin

from .models import Court


@admin.register(Court)
class CourtAdmin(admin.ModelAdmin):
    list_display = ("name", "court_type", "price_off_peak", "price_peak", "is_active", "sort_order")
    list_editable = ("price_off_peak", "price_peak", "is_active", "sort_order")
    prepopulated_fields = {"slug": ("name",)}
