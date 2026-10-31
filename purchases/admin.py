from django.contrib import admin
from core.admin import SuperuserReadOnlyAdmin

from .models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
)


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(SuperuserReadOnlyAdmin):
    list_display = ('order_number', 'supplier', 'branch', 'order_date', 'status')
    list_filter = ('status', 'branch')
    search_fields = ('order_number', 'supplier__name')


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(SuperuserReadOnlyAdmin):
    list_display = ('receipt_number', 'order', 'warehouse', 'receipt_date', 'status')
    list_filter = ('status', 'branch', 'warehouse')
    search_fields = ('receipt_number', 'order__order_number')


@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(SuperuserReadOnlyAdmin):
    list_display = ('invoice_number', 'supplier', 'branch', 'invoice_date', 'status', 'total_amount')
    list_filter = ('status', 'branch')
    search_fields = ('invoice_number', 'supplier__name')


for model in (Supplier, PurchaseOrderLine, GoodsReceiptLine, PurchaseInvoiceLine):
    admin.site.register(model, SuperuserReadOnlyAdmin)
