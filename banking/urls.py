from django.urls import path

from .views import (
    bank_account_create,
    bank_import_csv,
    bank_transaction_create,
    bank_transaction_detail,
    bank_transaction_allocate,
    banking_home,
    allocation_reverse,
    invoice_balance_register,
    reconcile_transactions,
    unreconcile_transaction,
)

urlpatterns = [
    path('', banking_home, name='banking_home'),
    path('accounts/new/', bank_account_create, name='bank_account_create'),
    path('transactions/new/', bank_transaction_create, name='bank_transaction_create'),
    path('transactions/<int:pk>/', bank_transaction_detail, name='bank_transaction_detail'),
    path('transactions/<int:pk>/allocate/', bank_transaction_allocate, name='bank_transaction_allocate'),
    path('allocations/<int:pk>/reverse/', allocation_reverse, name='allocation_reverse'),
    path('receivables/', invoice_balance_register, {'kind': 'receivables'}, name='receivables_register'),
    path('payables/', invoice_balance_register, {'kind': 'payables'}, name='payables_register'),
    path('reconcile/', reconcile_transactions, name='bank_reconcile'),
    path('transactions/<int:pk>/unreconcile/', unreconcile_transaction, name='bank_unreconcile'),
    path('import/', bank_import_csv, name='bank_import_csv'),
]
