from django.urls import path

from .views import (
    sales_delivery_create,
    sales_invoice_create,
    sales_invoice_detail,
    sales_invoice_from_delivery,
    sales_invoice_list,
    sales_order_create,
    sales_order_detail,
    sales_order_list,
)

urlpatterns = [
    path('', sales_invoice_list, name='sales_invoice_list'),
    path('invoices/new/', sales_invoice_create, name='sales_invoice_create'),
    path('invoices/<int:pk>/', sales_invoice_detail, name='sales_invoice_detail'),
    path('orders/', sales_order_list, name='sales_order_list'),
    path('orders/new/', sales_order_create, name='sales_order_create'),
    path('orders/<int:pk>/', sales_order_detail, name='sales_order_detail'),
    path('orders/<int:pk>/deliveries/new/', sales_delivery_create, name='sales_delivery_create'),
    path('deliveries/<int:pk>/invoice/', sales_invoice_from_delivery, name='sales_invoice_from_delivery'),
]
