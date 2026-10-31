from django.urls import path

from .views import (
    customer_return_create,
    customer_return_detail,
    customer_return_edit,
    customer_return_list,
    supplier_return_create_from_invoice,
    supplier_return_create_from_receipt,
    supplier_return_detail,
    supplier_return_edit,
    supplier_return_list,
)

urlpatterns = [
    path('customers/', customer_return_list, name='customer_return_list'),
    path('customers/new/<int:invoice_pk>/', customer_return_create, name='customer_return_create'),
    path('customers/<int:pk>/', customer_return_detail, name='customer_return_detail'),
    path('customers/<int:pk>/edit/', customer_return_edit, name='customer_return_edit'),
    path('suppliers/', supplier_return_list, name='supplier_return_list'),
    path('suppliers/new/receipt/<int:receipt_pk>/', supplier_return_create_from_receipt, name='supplier_return_create_from_receipt'),
    path('suppliers/new/invoice/<int:invoice_pk>/', supplier_return_create_from_invoice, name='supplier_return_create_from_invoice'),
    path('suppliers/<int:pk>/', supplier_return_detail, name='supplier_return_detail'),
    path('suppliers/<int:pk>/edit/', supplier_return_edit, name='supplier_return_edit'),
]
