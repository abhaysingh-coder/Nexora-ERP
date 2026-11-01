from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import DecimalField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.utils import timezone

from accounting.models import JournalEntry, JournalEntryLine
from banking.models import BankAccount
from core.organization_context import get_active_organization
from inventory.valuation import summarize_inventory_valuation
from purchases.models import PurchaseInvoice
from sales.models import SalesInvoice


@login_required
def dashboard_home(request):
    organization = get_active_organization(request)
    if organization is None:
        sales_total = Decimal('0')
        purchase_total = Decimal('0')
        cash_balance = Decimal('0')
        receivables = Decimal('0')
        payables = Decimal('0')
        shared_receivables = Decimal('0')
        shared_payables = Decimal('0')
        inventory_summary = {
            'value': Decimal('0.00'),
            'is_complete': True,
            'exception_count': 0,
        }
        low_stock_count = 0
        recent_movements = []
        shared_recent_movements = []
    else:
        invoice_scope = {'organization': organization}
        if request.user.branch_id:
            invoice_scope['branch_id'] = request.user.branch_id
        sales_total = SalesInvoice.objects.filter(
            **invoice_scope,
            status='posted',
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        purchase_total = PurchaseInvoice.objects.filter(
            **invoice_scope,
            status='posted',
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        bank_accounts = BankAccount.objects.filter(organization=organization, is_active=True)
        if request.user.branch_id:
            bank_accounts = bank_accounts.filter(branch_id=request.user.branch_id)
        cash_balance = bank_accounts.aggregate(total=Sum('current_balance'))['total'] or Decimal('0')
        posted_lines = JournalEntryLine.objects.filter(
            entry__organization=organization,
            entry__status='posted',
        )
        if request.user.branch_id:
            posted_lines = posted_lines.filter(
                Q(entry__branch_id=request.user.branch_id) | Q(entry__branch__isnull=True)
            )
        receivable_lines = posted_lines.filter(account__code='1100')
        payable_lines = posted_lines.filter(account__code='2000')
        shared_receivable_lines = receivable_lines.filter(entry__branch__isnull=True)
        shared_payable_lines = payable_lines.filter(entry__branch__isnull=True)
        if request.user.branch_id:
            receivable_lines = receivable_lines.filter(entry__branch_id=request.user.branch_id)
            payable_lines = payable_lines.filter(entry__branch_id=request.user.branch_id)
            shared_receivables = (
                (shared_receivable_lines.aggregate(total=Sum('debit'))['total'] or Decimal('0'))
                - (shared_receivable_lines.aggregate(total=Sum('credit'))['total'] or Decimal('0'))
            )
            shared_payables = (
                (shared_payable_lines.aggregate(total=Sum('credit'))['total'] or Decimal('0'))
                - (shared_payable_lines.aggregate(total=Sum('debit'))['total'] or Decimal('0'))
            )
        else:
            shared_receivables = Decimal('0')
            shared_payables = Decimal('0')
        receivables = (
            (receivable_lines.aggregate(total=Sum('debit'))['total'] or Decimal('0'))
            - (receivable_lines.aggregate(total=Sum('credit'))['total'] or Decimal('0'))
        )
        payables = (
            (payable_lines.aggregate(total=Sum('credit'))['total'] or Decimal('0'))
            - (payable_lines.aggregate(total=Sum('debit'))['total'] or Decimal('0'))
        )
        from inventory.models import Product

        product_scope = Product.objects.filter(organization=organization)
        if request.user.branch_id:
            from inventory.models import StockMovement

            branch_quantity = StockMovement.objects.filter(
                product_id=OuterRef('pk'),
                warehouse__branch_id=request.user.branch_id,
            ).order_by().values('product_id').annotate(total=Sum('quantity')).values('total')[:1]
            product_scope = product_scope.annotate(
                available_quantity=Coalesce(
                    Subquery(branch_quantity, output_field=DecimalField(max_digits=18, decimal_places=3)),
                    Value(Decimal('0')),
                    output_field=DecimalField(max_digits=18, decimal_places=3),
                )
            )
            low_stock_count = product_scope.filter(available_quantity__lt=F('reorder_level')).count()
        else:
            low_stock_count = product_scope.filter(stock_quantity__lt=F('reorder_level')).count()
        inventory_summary = summarize_inventory_valuation(
            organization,
            timezone.localdate(),
            branch=request.user.branch if request.user.branch_id else None,
        )
        recent_movements = list(
            JournalEntry.objects.filter(
                organization=organization,
                status='posted',
                **({'branch_id': request.user.branch_id} if request.user.branch_id else {}),
            ).select_related('branch', 'created_by').order_by('-entry_date')[:5]
        )
        shared_recent_movements = (
            list(
                JournalEntry.objects.filter(
                    organization=organization,
                    status='posted',
                    branch__isnull=True,
                ).select_related('created_by').order_by('-entry_date')[:5]
            )
            if request.user.branch_id
            else []
        )

    context = {
        'sales_total': sales_total,
        'purchase_total': purchase_total,
        'cash_balance': cash_balance,
        'receivables': receivables,
        'payables': payables,
        'shared_receivables': shared_receivables,
        'shared_payables': shared_payables,
        'has_branch_scope': bool(request.user.branch_id),
        'inventory_value': inventory_summary['value'],
        'inventory_value_complete': inventory_summary['is_complete'],
        'inventory_valuation_exceptions': inventory_summary['exception_count'],
        'low_stock_count': low_stock_count,
        'recent_movements': recent_movements,
        'shared_recent_movements': shared_recent_movements,
        'active_page': 'dashboard',
        'organization': organization,
    }
    return render(request, 'dashboard/index.html', context)
