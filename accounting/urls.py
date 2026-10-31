from django.urls import path

from .views import (
    accounting_periods,
    balance_sheet,
    budget_report,
    fixed_assets,
    general_ledger,
    journal_entry_detail,
    profit_and_loss,
    trial_balance,
    year_end_closing,
)

urlpatterns = [
    path('reports/trial-balance/', trial_balance, name='trial_balance'),
    path('reports/general-ledger/', general_ledger, name='general_ledger'),
    path('journal-entries/<int:pk>/', journal_entry_detail, name='journal_entry_detail'),
    path('reports/profit-and-loss/', profit_and_loss, name='profit_and_loss'),
    path('reports/balance-sheet/', balance_sheet, name='balance_sheet'),
    path('periods/', accounting_periods, name='accounting_periods'),
    path('year-end/<int:year_id>/', year_end_closing, name='year_end_closing'),
    path('budgets/', budget_report, name='budget_report'),
    path('fixed-assets/', fixed_assets, name='fixed_assets'),
]
