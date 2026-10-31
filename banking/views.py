import csv
from decimal import Decimal
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseForbidden
from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import get_valid_filename
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.conf import settings
from django.utils.dateparse import parse_date

from .forms import (
    AllocationReversalForm,
    BankAccountForm,
    BankTransactionForm,
    InvoiceAllocationForm,
)
from .models import (
    BankAccount,
    BankReconciliationEvent,
    BankStatementImport,
    BankStatementLine,
    BankTransaction,
    InvoiceAllocation,
)
from .services import allocate_payment, reverse_allocation, transaction_unapplied_amount
from core.organization_context import get_active_organization
from core.permissions import has_organization_role
from .permissions import BANKING_OPERATION_ROLES, can_manage_banking_operations


def get_user_organization(request):
    return get_active_organization(request)


def _accounts_for_user(request, organization):
    accounts = BankAccount.objects.filter(organization=organization)
    if request.user.branch_id:
        accounts = accounts.filter(Q(branch_id=request.user.branch_id) | Q(branch__isnull=True))
    return accounts


def _can_manage_allocations(user, organization, *, reverse=False):
    roles = {'owner', 'admin', 'accountant', 'finance', 'finance_manager', 'manager', 'bookkeeper'}
    if reverse:
        roles = BANKING_OPERATION_ROLES
    return has_organization_role(user, organization, roles)


def _allocation_candidates(bank_transaction):
    from purchases.models import PurchaseInvoice
    from sales.models import SalesInvoice

    if bank_transaction.transaction_type == 'receipt' and bank_transaction.customer_id:
        invoices = SalesInvoice.objects.filter(
            organization_id=bank_transaction.organization_id,
            customer_id=bank_transaction.customer_id,
            status='posted',
        )
        relation = 'receipt_allocations'
    elif bank_transaction.transaction_type == 'payment' and bank_transaction.supplier_id:
        invoices = PurchaseInvoice.objects.filter(
            organization_id=bank_transaction.organization_id,
            supplier_id=bank_transaction.supplier_id,
            status='posted',
        )
        relation = 'payment_allocations'
    else:
        return []
    if bank_transaction.branch_id:
        invoices = invoices.filter(Q(branch_id=bank_transaction.branch_id) | Q(branch__isnull=True))
    return list(
        invoices.annotate(
            allocated_amount=Coalesce(
                Sum(
                    f'{relation}__amount',
                    filter=Q(**{f'{relation}__reversed_at__isnull': True}),
                ),
                Value(Decimal('0')),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            )
        ).annotate(
            remaining_amount=F('total_amount') - F('allocated_amount')
        ).filter(total_amount__gt=F('allocated_amount')).order_by('due_date', 'invoice_date', 'id')[:100]
    )


@login_required
def banking_home(request):
    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    accounts = _accounts_for_user(request, organization).order_by('name')
    transactions = BankTransaction.objects.filter(
        organization=organization,
    ).select_related('account', 'related_account')
    if request.user.branch_id:
        transactions = transactions.filter(branch_id=request.user.branch_id)
    query = request.GET.get('q')
    if query:
        transactions = transactions.filter(Q(reference_number__icontains=query) | Q(description__icontains=query))
    paginator = Paginator(transactions, 10)
    page_obj = paginator.get_page(request.GET.get('page'))
    context = {
        'organization': organization,
        'accounts': accounts,
        'transactions': page_obj,
        'can_manage_banking_operations': can_manage_banking_operations(request.user, organization),
        'active_page': 'banking',
    }
    return render(request, 'banking/index.html', context)


@login_required
def bank_account_create(request):
    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not can_manage_banking_operations(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create bank accounts.')
    if request.method == 'POST':
        form = BankAccountForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.organization = organization
            obj.branch = request.user.branch
            obj.save()
            messages.success(request, 'Bank account created successfully.')
            return redirect('banking_home')
    else:
        form = BankAccountForm()
    return render(request, 'banking/account_form.html', {'form': form, 'organization': organization, 'active_page': 'banking'})


@login_required
def bank_transaction_create(request):
    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not can_manage_banking_operations(request.user, organization):
        return HttpResponseForbidden('You do not have permission to post banking transactions.')
    if request.method == 'POST':
        form = BankTransactionForm(request.POST, organization=organization, branch=request.user.branch)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.organization = organization
            obj.branch = request.user.branch
            obj.created_by = request.user
            try:
                with transaction.atomic():
                    obj.save()
                    obj.post()
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, 'Bank transaction posted successfully.')
                return redirect('bank_transaction_detail', pk=obj.pk)
    else:
        form = BankTransactionForm(organization=organization, branch=request.user.branch)
    return render(request, 'banking/transaction_form.html', {'form': form, 'organization': organization, 'active_page': 'banking'})


@login_required
def bank_transaction_detail(request, pk):
    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    transactions = BankTransaction.objects.select_related('account', 'related_account', 'customer', 'supplier').filter(
        organization=organization
    )
    if request.user.branch_id:
        transactions = transactions.filter(branch_id=request.user.branch_id)
    transaction = get_object_or_404(transactions, pk=pk)
    candidates = _allocation_candidates(transaction)
    form = InvoiceAllocationForm(bank_transaction=transaction)
    form.fields['invoice'].queryset = form.fields['invoice'].queryset.filter(
        pk__in=[invoice.pk for invoice in candidates]
    )
    allocations = transaction.invoice_allocations.select_related(
        'sales_invoice', 'purchase_invoice', 'created_by', 'reversed_by'
    ).order_by('-created_at')
    return render(
        request,
        'banking/transaction_detail.html',
        {
            'transaction': transaction,
            'organization': organization,
            'allocations': allocations,
            'available_amount': transaction_unapplied_amount(transaction),
            'allocation_candidates': candidates,
            'allocation_form': form,
            'reversal_form': AllocationReversalForm(),
            'can_manage_allocations': _can_manage_allocations(request.user, organization),
            'can_reverse_allocations': _can_manage_allocations(request.user, organization, reverse=True),
            'active_page': 'banking',
        },
    )


@login_required
def bank_transaction_allocate(request, pk):
    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if request.method != 'POST':
        return HttpResponseForbidden('Allocation changes must be submitted using POST.')
    if not _can_manage_allocations(request.user, organization):
        return HttpResponseForbidden('You do not have permission to allocate receipts or payments.')
    transaction_scope = BankTransaction.objects.filter(organization=organization)
    if request.user.branch_id:
        transaction_scope = transaction_scope.filter(branch_id=request.user.branch_id)
    bank_transaction = get_object_or_404(transaction_scope, pk=pk)
    form = InvoiceAllocationForm(request.POST, bank_transaction=bank_transaction)
    form.fields['invoice'].queryset = form.fields['invoice'].queryset.filter(
        pk__in=[invoice.pk for invoice in _allocation_candidates(bank_transaction)]
    )
    if form.is_valid():
        try:
            invoice = form.cleaned_data['invoice']
            allocation = allocate_payment(
                bank_transaction_id=bank_transaction.pk,
                invoice_id=invoice.pk,
                amount=form.cleaned_data['amount'],
                user=request.user,
            )
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, f'Allocated {allocation.amount} to invoice {allocation.invoice}.')
    else:
        messages.error(request, '; '.join(
            message for errors in form.errors.values() for message in errors
        ))
    return redirect('bank_transaction_detail', pk=bank_transaction.pk)


@login_required
def allocation_reverse(request, pk):
    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if request.method != 'POST':
        return HttpResponseForbidden('Allocation reversals must be submitted using POST.')
    if not _can_manage_allocations(request.user, organization, reverse=True):
        return HttpResponseForbidden('You do not have permission to reverse allocations.')
    scope = InvoiceAllocation.objects.filter(bank_transaction__organization=organization)
    if request.user.branch_id:
        scope = scope.filter(bank_transaction__branch_id=request.user.branch_id)
    allocation = get_object_or_404(scope, pk=pk)
    form = AllocationReversalForm(request.POST)
    if form.is_valid():
        try:
            reverse_allocation(
                allocation_id=allocation.pk,
                user=request.user,
                reason=form.cleaned_data['reason'],
            )
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, 'Allocation reversed. Its history has been retained.')
    else:
        messages.error(request, '; '.join(
            message for errors in form.errors.values() for message in errors
        ))
    return redirect('bank_transaction_detail', pk=allocation.bank_transaction_id)


@login_required
def invoice_balance_register(request, kind):
    from purchases.models import PurchaseInvoice
    from sales.models import SalesInvoice

    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if kind == 'receivables':
        invoice_model = SalesInvoice
        party_field = 'customer'
        related_name = 'receipt_allocations'
        party_name = 'customer__name'
        title = 'Receivables and customer statements'
        parties = organization.customers.all().order_by('name')
    elif kind == 'payables':
        invoice_model = PurchaseInvoice
        party_field = 'supplier'
        related_name = 'payment_allocations'
        party_name = 'supplier__name'
        title = 'Payables and supplier statements'
        parties = organization.suppliers.all().order_by('name')
    else:
        return HttpResponseForbidden('Unknown financial register.')

    raw_start = request.GET.get('start', '')
    raw_end = request.GET.get('end', '')
    start = parse_date(raw_start)
    end = parse_date(raw_end)
    error = ''
    if (raw_start and start is None) or (raw_end and end is None):
        error = 'Enter dates in YYYY-MM-DD format.'
    elif start and end and start > end:
        error = 'Start date must not be after end date.'

    invoices = invoice_model.objects.filter(organization=organization, status='posted')
    if request.user.branch_id:
        invoices = invoices.filter(Q(branch_id=request.user.branch_id) | Q(branch__isnull=True))
    party_id = request.GET.get('party', '')
    if party_id:
        invoices = invoices.filter(**{f'{party_field}_id': party_id})
    if start:
        invoices = invoices.filter(invoice_date__gte=start)
    if end:
        invoices = invoices.filter(invoice_date__lte=end)
    invoices = invoices.annotate(
        allocated_amount=Coalesce(
            Sum(
                f'{related_name}__amount',
                filter=Q(**{f'{related_name}__reversed_at__isnull': True}),
            ),
            Value(Decimal('0')),
            output_field=DecimalField(max_digits=18, decimal_places=2),
        )
    ).filter(total_amount__gt=F('allocated_amount')).order_by('due_date', 'invoice_date', 'id')
    advance_type = 'receipt' if kind == 'receivables' else 'payment'
    advance_party_field = 'customer_id' if kind == 'receivables' else 'supplier_id'
    advances = BankTransaction.objects.filter(
        organization=organization,
        transaction_type=advance_type,
        status__in=['posted', 'reconciled'],
        settlement_mode='advance',
        **({advance_party_field: party_id} if party_id else {}),
    )
    if request.user.branch_id:
        advances = advances.filter(branch_id=request.user.branch_id)
    advances = advances.select_related('customer', 'supplier').annotate(
        allocated_amount=Coalesce(
            Sum(
                'invoice_allocations__amount',
                filter=Q(invoice_allocations__reversed_at__isnull=True),
            ),
            Value(Decimal('0')),
            output_field=DecimalField(max_digits=18, decimal_places=2),
        )
    ).annotate(
        unapplied_amount=F('amount') - F('allocated_amount')
    ).filter(amount__gt=F('allocated_amount')).order_by('-transaction_date', '-id')
    unapplied_total = advances.aggregate(total=Sum('unapplied_amount'))['total'] or Decimal('0')

    today = timezone.localdate()
    if error:
        invoices = invoices.none()
    if request.GET.get('format') == 'csv' and not error:
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{kind}.csv"'
        writer = csv.writer(response)
        writer.writerow(['Counterparty', 'Invoice', 'Invoice date', 'Due date', 'Original amount', 'Allocated', 'Outstanding', 'Days overdue', 'Ageing'])
        for invoice in invoices.iterator():
            days_overdue = max((today - invoice.due_date).days, 0) if invoice.due_date else 0
            writer.writerow([
                getattr(invoice, party_field).name,
                invoice.invoice_number,
                invoice.invoice_date,
                invoice.due_date or '',
                invoice.total_amount,
                invoice.allocated_amount,
                invoice.total_amount - invoice.allocated_amount,
                days_overdue,
                _ageing_bucket(invoice.due_date, today),
            ])
        writer.writerow([])
        writer.writerow(['Unapplied advances'])
        writer.writerow(['Counterparty', 'Reference', 'Date', 'Original amount', 'Allocated', 'Unapplied'])
        for advance in advances.iterator():
            party = advance.customer if kind == 'receivables' else advance.supplier
            writer.writerow([
                party.name,
                advance.reference_number,
                advance.transaction_date,
                advance.amount,
                advance.allocated_amount,
                advance.unapplied_amount,
            ])
        return response

    page = Paginator(invoices.select_related(party_field), 50).get_page(request.GET.get('page'))
    rows = []
    for invoice in page:
        days_overdue = max((today - invoice.due_date).days, 0) if invoice.due_date else 0
        rows.append({
            'counterparty': getattr(invoice, party_field),
            'invoice': invoice,
            'allocated': invoice.allocated_amount,
            'outstanding': invoice.total_amount - invoice.allocated_amount,
            'days_overdue': days_overdue,
            'ageing': _ageing_bucket(invoice.due_date, today),
        })
    return render(
        request,
        'banking/invoice_register.html',
        {
            'organization': organization,
            'title': title,
            'kind': kind,
            'parties': parties,
            'selected_party': party_id,
            'rows': page,
            'report_rows': rows,
            'start': start,
            'end': end,
            'error': error,
            'advances': advances[:50],
            'unapplied_total': unapplied_total,
            'active_page': 'banking',
        },
    )


def _ageing_bucket(due_date, today):
    if due_date is None or due_date >= today:
        return 'Not due'
    days = (today - due_date).days
    if days <= 30:
        return '1–30 days'
    if days <= 60:
        return '31–60 days'
    if days <= 90:
        return '61–90 days'
    return 'Over 90 days'


@login_required
def reconcile_transactions(request):
    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    can_manage_banking = can_manage_banking_operations(request.user, organization)
    if request.method == 'POST':
        if not can_manage_banking:
            return HttpResponseForbidden('You do not have permission to confirm bank statement matches.')
        transaction_id = request.POST.get('transaction_id')
        statement_line_id = request.POST.get('statement_line_id')
        transaction_scope = BankTransaction.objects.filter(organization=organization)
        line_scope = BankStatementLine.objects.filter(import_record__organization=organization)
        if request.user.branch_id:
            transaction_scope = transaction_scope.filter(branch_id=request.user.branch_id)
            line_scope = line_scope.filter(import_record__branch_id=request.user.branch_id)
        bank_transaction = get_object_or_404(transaction_scope, pk=transaction_id)
        statement_line = get_object_or_404(line_scope, pk=statement_line_id)
        try:
            bank_transaction.mark_reconciled(statement_line, user=request.user)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, 'Statement line and transaction were matched and reconciled.')
        return redirect('bank_reconcile')

    transactions = BankTransaction.objects.filter(
        organization=organization,
        status='posted',
        is_reconciled=False,
    ).select_related('account', 'related_account', 'customer', 'supplier').order_by('transaction_date')
    statement_lines = BankStatementLine.objects.filter(
        import_record__organization=organization,
        status='unmatched',
    ).select_related('account', 'import_record').order_by('statement_date')
    imports = BankStatementImport.objects.filter(organization=organization).select_related(
        'account'
    ).order_by('-imported_at')
    if request.user.branch_id:
        transactions = transactions.filter(branch_id=request.user.branch_id)
        statement_lines = statement_lines.filter(import_record__branch_id=request.user.branch_id)
        imports = imports.filter(branch_id=request.user.branch_id)
    suggestion_days = max(int(getattr(settings, 'BANK_RECONCILIATION_DATE_TOLERANCE_DAYS', 3)), 0)
    statement_line_items = []
    for line in statement_lines:
        start_date = line.statement_date - timedelta(days=suggestion_days)
        end_date = line.statement_date + timedelta(days=suggestion_days)
        candidates = transactions.filter(
            account_id=line.account_id,
            transaction_date__range=(start_date, end_date),
            amount=abs(line.amount),
        )
        if line.amount < 0:
            candidates = candidates.filter(transaction_type__in=['payment', 'transfer'])
        else:
            candidates = candidates.filter(transaction_type='receipt')
        scored = []
        for candidate in candidates[:20]:
            reasons = ['same bank account', 'same amount and direction']
            score = 60
            day_distance = abs((candidate.transaction_date - line.statement_date).days)
            if suggestion_days:
                score += int(20 * (suggestion_days - day_distance) / suggestion_days)
            elif day_distance == 0:
                score += 20
            if line.reference.casefold() in {
                candidate.reference_number.casefold(),
                candidate.payment_reference.casefold(),
            }:
                score += 15
                reasons.append('reference matches')
            party_name = (
                candidate.customer.name if candidate.customer_id
                else candidate.supplier.name if candidate.supplier_id
                else ''
            )
            if party_name and party_name.casefold() in line.description.casefold():
                score += 5
                reasons.append('counterparty appears in statement description')
            if day_distance:
                reasons.append(f'date is {day_distance} day(s) apart')
            else:
                reasons.append('same date')
            scored.append({
                'transaction': candidate,
                'confidence': min(score, 100),
                'reasons': reasons,
            })
        statement_line_items.append({
            'line': line,
            'suggestions': sorted(scored, key=lambda item: item['confidence'], reverse=True),
        })
    reconciled = BankTransaction.objects.filter(
        organization=organization,
        status='reconciled',
    ).select_related('account', 'statement_match')
    if request.user.branch_id:
        reconciled = reconciled.filter(branch_id=request.user.branch_id)
    return render(
        request,
        'banking/reconcile.html',
        {
            'transactions': transactions,
            'statement_lines': statement_line_items,
            'reconciled_transactions': reconciled.order_by('-transaction_date')[:50],
            'suggestion_days': suggestion_days,
            'imports': imports[:10],
            'organization': organization,
            'can_manage_banking_operations': can_manage_banking,
            'active_page': 'banking',
        },
    )


@login_required
def unreconcile_transaction(request, pk):
    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if request.method != 'POST':
        return HttpResponseForbidden('Reconciliation changes must be submitted using POST.')
    if not _can_manage_allocations(request.user, organization, reverse=True):
        return HttpResponseForbidden('You do not have permission to undo reconciliation.')
    scope = BankTransaction.objects.filter(organization=organization)
    if request.user.branch_id:
        scope = scope.filter(branch_id=request.user.branch_id)
    bank_transaction = get_object_or_404(scope, pk=pk)
    try:
        bank_transaction.mark_unreconciled(
            user=request.user,
            reason=request.POST.get('reason', ''),
        )
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    else:
        messages.success(request, 'Reconciliation was undone and the audit history was retained.')
    return redirect('bank_reconcile')


@login_required
def bank_import_csv(request):
    organization = get_user_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not can_manage_banking_operations(request.user, organization):
        return HttpResponseForbidden('You do not have permission to import bank statements.')
    if request.method == 'POST':
        account_id = request.POST.get('account')
        if not account_id:
            messages.error(request, 'Select a bank account before importing.')
            return redirect('banking_home')
        account = get_object_or_404(_accounts_for_user(request, organization), pk=account_id)
        uploaded = request.FILES.get('csv_file')
        if not uploaded:
            messages.error(request, 'Please upload a CSV file.')
            return redirect('banking_home')
        if uploaded.size > 5 * 1024 * 1024:
            messages.error(request, 'CSV files must be no larger than 5 MB.')
            return redirect('bank_import_csv')
        try:
            csv_text = uploaded.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            messages.error(request, 'CSV file must be UTF-8 encoded.')
            return redirect('bank_import_csv')
        statement = BankStatementImport.objects.create(
            organization=organization,
            branch=request.user.branch,
            account=account,
            file_name=get_valid_filename(uploaded.name)[:255],
            uploaded_by=request.user,
        )
        try:
            created, duplicates = statement.process_csv(csv_text)
        except ValidationError as exc:
            statement.status = 'failed'
            statement.save(update_fields=['status'])
            messages.error(request, '; '.join(exc.messages))
            return redirect('bank_import_csv')
        messages.success(request, f'Imported {created} rows with {duplicates} duplicates ignored.')
        return redirect('bank_reconcile')
    return render(
        request,
        'banking/import_form.html',
        {
            'organization': organization,
            'accounts': _accounts_for_user(request, organization).filter(is_active=True),
            'active_page': 'banking',
        },
    )
