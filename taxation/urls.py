from django.urls import path

from .views import (
    taxation_home,
    tax_code_create,
    tax_rate_create,
    tax_registration_create,
    product_tax_classification_create,
    sales_tax_register,
    purchase_tax_register,
    output_tax_summary,
    input_tax_summary,
    tax_register_csv,
    tax_liability_summary,
)

urlpatterns = [
    path('', taxation_home, name='taxation_home'),
    path('tax-codes/new/', tax_code_create, name='tax_code_create'),
    path('tax-rates/new/', tax_rate_create, name='tax_rate_create'),
    path('registrations/new/', tax_registration_create, name='tax_registration_create'),
    path('product-tax/new/', product_tax_classification_create, name='product_tax_classification_create'),
    path('register/sales/', sales_tax_register, name='sales_tax_register'),
    path('register/purchases/', purchase_tax_register, name='purchase_tax_register'),
    path('summary/output/', output_tax_summary, name='output_tax_summary'),
    path('summary/input/', input_tax_summary, name='input_tax_summary'),
    path('summary/liability/', tax_liability_summary, name='tax_liability_summary'),
    path('register/export.csv', tax_register_csv, name='tax_register_csv'),
]
