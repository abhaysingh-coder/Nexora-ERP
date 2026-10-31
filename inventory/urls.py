from django.urls import path

from .views import (
    inventory_home,
    inventory_opening_valuation,
    inventory_valuation,
    product_create,
    stock_adjustment,
    stock_transfer,
    warehouse_create,
)

urlpatterns = [
    path('', inventory_home, name='inventory_home'),
    path('products/new/', product_create, name='product_create'),
    path('warehouses/new/', warehouse_create, name='warehouse_create'),
    path('adjustments/new/', stock_adjustment, name='stock_adjustment'),
    path('transfers/new/', stock_transfer, name='stock_transfer'),
    path('valuation/', inventory_valuation, name='inventory_valuation'),
    path('valuation/opening/', inventory_opening_valuation, name='inventory_opening_valuation'),
]
