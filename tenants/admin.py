from django.contrib import admin

from .models import Club, Membership


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    raw_id_fields = ("user",)


@admin.register(Club)
class ClubAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "custom_domain", "plan", "trial_ends_at", "paid_until", "is_suspended")
    list_filter = ("plan", "is_suspended")
    search_fields = ("name", "slug", "custom_domain")
    inlines = [MembershipInline]
