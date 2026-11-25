from django.contrib import admin

from .models import Booking, BookingExtra, Extra, PromoCode


class BookingExtraInline(admin.TabularInline):
    model = BookingExtra
    extra = 0


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("reference", "court", "start_at", "duration_minutes", "customer_name", "customer_phone",
                    "status", "source")
    list_filter = ("status", "source", "court", "business_date")
    search_fields = ("reference", "customer_name", "customer_phone", "customer_email")
    date_hierarchy = "business_date"
    readonly_fields = ("reference", "created_at", "updated_at")
    inlines = [BookingExtraInline]


@admin.register(Extra)
class ExtraAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "max_quantity", "is_active", "sort_order")
    list_editable = ("price", "is_active", "sort_order")


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "discount_type", "value", "times_used", "max_uses", "valid_until", "is_active")
    list_filter = ("is_active", "discount_type")
    search_fields = ("code",)
