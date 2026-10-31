from django.contrib import admin
from core.admin import SuperuserReadOnlyAdmin

from .models import (
    CustomerReturn,
    CustomerReturnLine,
    SalesCreditNote,
    SalesCreditNoteLine,
    SupplierDebitNote,
    SupplierDebitNoteLine,
    SupplierReturn,
    SupplierReturnLine,
)


@admin.register(CustomerReturn)
class CustomerReturnAdmin(SuperuserReadOnlyAdmin):
    list_display = ('return_number', 'return_date', 'invoice', 'status', 'created_by')
    list_filter = ('organization', 'branch', 'status')
    search_fields = ('return_number', 'invoice__invoice_number', 'invoice__customer__name')
    readonly_fields = ('status', 'posted_at', 'created_at')


@admin.register(SupplierReturn)
class SupplierReturnAdmin(SuperuserReadOnlyAdmin):
    list_display = ('return_number', 'return_date', 'supplier', 'status', 'created_by')
    list_filter = ('organization', 'branch', 'status')
    search_fields = ('return_number', 'supplier__name', 'goods_receipt__receipt_number', 'purchase_invoice__invoice_number')
    readonly_fields = ('status', 'posted_at', 'created_at')


@admin.register(SalesCreditNote)
class SalesCreditNoteAdmin(SuperuserReadOnlyAdmin):
    list_display = ('credit_note_number', 'credit_date', 'customer', 'total_amount', 'status')
    list_filter = ('organization', 'branch', 'status')
    readonly_fields = tuple(field.name for field in SalesCreditNote._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SupplierDebitNote)
class SupplierDebitNoteAdmin(SuperuserReadOnlyAdmin):
    list_display = ('debit_note_number', 'debit_date', 'supplier', 'total_amount', 'status')
    list_filter = ('organization', 'branch', 'status')
    readonly_fields = tuple(field.name for field in SupplierDebitNote._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in (CustomerReturnLine, SupplierReturnLine, SalesCreditNoteLine, SupplierDebitNoteLine):
    admin.site.register(model, SuperuserReadOnlyAdmin)
