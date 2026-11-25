from django.contrib import admin

from .models import Invoice, InvoiceItem, InvoiceSequence, Payment


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "bill_to_name", "issue_date", "total", "amount_paid", "status")
    list_filter = ("status", "issue_date")
    search_fields = ("number", "bill_to_name", "bill_to_phone", "booking__reference")
    readonly_fields = ("number", "token", "subtotal", "tax_amount", "total", "amount_paid")
    inlines = [InvoiceItemInline, PaymentInline]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalculate()


admin.site.register(InvoiceSequence)


from .models import CreditPackage, DayClose, Wallet, WalletTransaction  # noqa: E402


class WalletTransactionInline(admin.TabularInline):
    model = WalletTransaction
    extra = 0
    readonly_fields = ("kind", "amount", "balance_after", "invoice", "note", "created_by", "created_at")


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "balance", "club", "updated_at")
    search_fields = ("name", "phone")
    readonly_fields = ("balance",)  # change balances through the dashboard so every change is logged
    inlines = [WalletTransactionInline]


admin.site.register(CreditPackage)
admin.site.register(DayClose)
