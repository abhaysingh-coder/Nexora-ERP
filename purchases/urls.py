from django.urls import path

from .views import (
    goods_receipt_create,
    goods_receipt_detail,
    goods_receipt_edit,
    purchase_invoice_create,
    purchase_invoice_detail,
    purchase_invoice_from_receipt,
    purchase_invoice_list,
    purchase_order_create,
    purchase_order_detail,
    purchase_order_edit,
    purchase_order_list,
)

urlpatterns = [
    path('', purchase_invoice_list, name='purchase_invoice_list'),
    path('invoices/new/', purchase_invoice_create, name='purchase_invoice_create'),
    path('invoices/<int:pk>/', purchase_invoice_detail, name='purchase_invoice_detail'),
    path('orders/', purchase_order_list, name='purchase_order_list'),
    path('orders/new/', purchase_order_create, name='purchase_order_create'),
    path('orders/<int:pk>/edit/', purchase_order_edit, name='purchase_order_edit'),
    path('orders/<int:pk>/', purchase_order_detail, name='purchase_order_detail'),
    path('orders/<int:order_pk>/receipts/new/', goods_receipt_create, name='goods_receipt_create'),
    path('receipts/<int:pk>/edit/', goods_receipt_edit, name='goods_receipt_edit'),
    path('receipts/<int:pk>/', goods_receipt_detail, name='goods_receipt_detail'),
    path('receipts/<int:receipt_pk>/invoice/', purchase_invoice_from_receipt, name='purchase_invoice_from_receipt'),
]
