import csv
import calendar
from datetime import timedelta
from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, DecimalField, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import render
from django.shortcuts import get_object_or_404, redirect
from django.utils.dateparse import parse_date
from django.contrib import messages

from .forms import (
    AssetDisposalForm,
    AssetTransferForm,
    BudgetForm,
    FinancialYearForm,
    FixedAssetCategoryForm,
    FixedAssetForm,
    PeriodReopenForm,
    YearEndCloseForm,
)
from .models import (
    Account,
    AccountingBudget,
    AccountingPeriod,
    FinancialYear,
    FixedAsset,
    FixedAssetCategory,
    JournalEntry,
    JournalEntryLine,
)
from .services import (
    dispose_fixed_asset,
    post_asset_depreciation,
    register_fixed_asset,
    transfer_fixed_asset,
    close_financial_year,
    preview_year_end_close,
)
from core.organization_context import get_active_organization
from core.permissions import has_organization_role


def _authorized_organization(request):
    return get_active_organization(request)


def _can_manage_periods(user, organization):
    return user.branch_id is None and _has_financial_role(user, organization)


def _has_financial_role(user, organization):
    return has_organization_role(
        user, organization, {'owner', 'admin', 'accountant', 'finance_manager'}
    )


def _report_lines(request, organization, *, include_start=True):
    lines = JournalEntryLine.objects.filter(
        entry__organization=organization,
        entry__status='posted',
    ).select_related('entry', 'account', 'entry__branch')
    if request.user.branch_id:
        lines = lines.filter(Q(entry__branch_id=request.user.branch_id) | Q(entry__branch__isnull=True))
    raw_start = request.GET.get('start', '')
    raw_end = request.GET.get('end', '')
    try:
        start = parse_date(raw_start)
    except ValueError:
        start = None
    try:
        end = parse_date(raw_end)
    except ValueError:
        end = None
    if (raw_start and start is None) or (raw_end and end is None):
        return lines.none(), start, end, 'Enter dates in YYYY-MM-DD format.'
    year_id = request.GET.get('financial_year')
    financial_year = None
    if year_id:
        financial_year = FinancialYear.objects.filter(
            organization=organization,
            pk=year_id,
        ).first()
        if financial_year is None:
            return lines.none(), start, end, 'Select a financial year belonging to this organization.'
        request.accounting_financial_year = financial_year
        start = start or financial_year.starts_on
        end = end or financial_year.ends_on
        if start < financial_year.starts_on or end > financial_year.ends_on:
            return lines.none(), start, end, 'Date filters must stay within the selected financial year.'
        lines = lines.filter(
            entry__entry_date__range=(financial_year.starts_on, financial_year.ends_on)
        )
    else:
        request.accounting_financial_year = None
        lines = lines.exclude(entry__year_end_link__role='opening')
    if start and include_start:
        lines = lines.filter(entry__entry_date__gte=start)
    if end:
        lines = lines.filter(entry__entry_date__lte=end)
    if start and end and start > end:
        return lines.none(), start, end, 'Start date must not be after end date.'
    return lines, start, end, ''


def _branch_report_lines(lines, branch_id):
    if not branch_id:
        return lines, None
    return (
        lines.filter(entry__branch_id=branch_id),
        lines.filter(entry__branch__isnull=True),
    )


def _account_summary_rows(aggregates, balance_mode):
    rows = []
    for item in aggregates:
        debit = item['debit'] or Decimal('0')
        credit = item['credit'] or Decimal('0')
        if balance_mode == 'net':
            balance = debit - credit
        elif item['account__account_type'] in {'asset', 'expense'}:
            balance = debit - credit
        else:
            balance = credit - debit
        rows.append(
            {
                'code': item['account__code'],
                'name': item['account__name'],
                'account_type': item['account__account_type'],
                'debit': debit,
                'credit': credit,
                'balance': balance,
            }
        )
    return rows


def _render_report(
    request,
    title,
    template,
    rows,
    organization,
    start,
    end,
    error='',
    note='',
    shared_rows=None,
):
    if request.GET.get('format') == 'csv' and not error:
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{title.lower().replace(" ", "-")}.csv"'
        writer = csv.writer(response)
        if shared_rows is None:
            writer.writerow(['Code', 'Account', 'Debit', 'Credit', 'Balance'])
        else:
            writer.writerow(['Scope', 'Code', 'Account', 'Debit', 'Credit', 'Balance'])
        for row in rows:
            values = [row['code'], row['name'], row['debit'], row['credit'], row['balance']]
            writer.writerow(values if shared_rows is None else ['Assigned branch', *values])
        for row in shared_rows or []:
            writer.writerow([
                'Shared / unallocated',
                row['code'],
                row['name'],
                row['debit'],
                row['credit'],
                row['balance'],
            ])
        return response
    return render(
        request,
        template,
        {
            'title': title,
            'organization': organization,
            'rows': Paginator(rows, 50).get_page(request.GET.get('page')),
            'shared_rows': (
                Paginator(shared_rows, 50).get_page(request.GET.get('shared_page'))
                if shared_rows is not None
                else None
            ),
            'has_shared_scope': shared_rows is not None,
            'start': start,
            'end': end,
            'error': error,
            'note': note,
            'financial_years': FinancialYear.objects.filter(
                organization=organization
            ).order_by('-starts_on'),
            'selected_financial_year': getattr(request, 'accounting_financial_year', None),
            'balance_label': {
                'Trial balance': 'Net balance (debit − credit)',
                'Profit and loss': 'Normal balance (income: credit − debit; expense: debit − credit)',
                'Balance sheet': 'Normal balance (asset: debit − credit; liability/equity: credit − debit)',
            }.get(title, 'Net balance'),
            'active_page': 'accounting',
        },
    )


@login_required
def journal_entry_detail(request, pk):
    organization = _authorized_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    entries = JournalEntry.objects.filter(organization=organization)
    if request.user.branch_id:
        entries = entries.filter(Q(branch_id=request.user.branch_id) | Q(branch__isnull=True))
    entry = get_object_or_404(entries.select_related('branch'), pk=pk)
    return render(request, 'accounting/journal_entry_detail.html', {
        'organization': organization,
        'entry': entry,
        'lines': entry.lines.select_related('account').order_by('id'),
        'active_page': 'accounting',
    })


def _general_ledger_rows(lines, opening_lines):
    opening_balances = {
        item['account_id']: (item['debit'] or Decimal('0')) - (item['credit'] or Decimal('0'))
        for item in opening_lines.values('account_id').annotate(
            debit=Sum('debit'),
            credit=Sum('credit'),
        )
    }
    running_balances = opening_balances
    rows = []
    for line in lines.order_by('entry__entry_date', 'entry_id', 'id'):
        running_balances[line.account_id] = (
            running_balances.get(line.account_id, Decimal('0')) + line.debit - line.credit
        )
        rows.append(
            {
                'date': line.entry.entry_date,
                'reference': line.entry.reference,
                'account': line.account,
                'description': line.description,
                'debit': line.debit,
                'credit': line.credit,
                'balance': running_balances[line.account_id],
            }
        )
    return rows


@login_required
def trial_balance(request):
    organization = _authorized_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    lines, start, end, error = _report_lines(request, organization, include_start=False)
    if end:
        lines = lines.filter(entry__entry_date__lte=end)
    start = None
    branch_lines, shared_lines = _branch_report_lines(lines, request.user.branch_id)
    aggregates = branch_lines.values('account_id', 'account__code', 'account__name', 'account__account_type').annotate(
        debit=Sum('debit'),
        credit=Sum('credit'),
    ).order_by('account__code')
    rows = _account_summary_rows(aggregates, 'net')
    shared_rows = None
    if shared_lines is not None:
        shared_aggregates = shared_lines.values(
            'account_id', 'account__code', 'account__name', 'account__account_type'
        ).annotate(debit=Sum('debit'), credit=Sum('credit')).order_by('account__code')
        shared_rows = _account_summary_rows(shared_aggregates, 'debit')
    return _render_report(
        request,
        'Trial balance',
        'accounting/report.html',
        rows,
        organization,
        start,
        end,
        error,
        note=(
            'Limited to the selected financial year and includes its opening balances.'
            if getattr(request, 'accounting_financial_year', None)
            else 'As of the end date; all earlier posted entries are included.'
        ),
        shared_rows=shared_rows,
    )


@login_required
def general_ledger(request):
    organization = _authorized_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    lines, start, end, error = _report_lines(request, organization)
    account_id = request.GET.get('account')
    accounts = Account.objects.filter(organization=organization, is_active=True).order_by('code')
    if request.user.branch_id:
        accounts = accounts.filter(Q(branch_id=request.user.branch_id) | Q(branch__isnull=True))
    if account_id:
        if not accounts.filter(pk=account_id).exists():
            return HttpResponseForbidden('Account is outside your organization or branch access.')
        lines = lines.filter(account_id=account_id)
    opening_lines = JournalEntryLine.objects.none()
    if start and not error:
        opening_lines = JournalEntryLine.objects.filter(
            entry__organization=organization,
            entry__status='posted',
            entry__entry_date__lt=start,
        )
        selected_year = getattr(request, 'accounting_financial_year', None)
        if selected_year:
            opening_lines = opening_lines.filter(
                entry__entry_date__gte=selected_year.starts_on,
            )
        else:
            opening_lines = opening_lines.exclude(entry__year_end_link__role='opening')
        if request.user.branch_id:
            opening_lines = opening_lines.filter(
                Q(entry__branch_id=request.user.branch_id) | Q(entry__branch__isnull=True)
            )
        if account_id:
            opening_lines = opening_lines.filter(account_id=account_id)
    if request.user.branch_id:
        branch_lines, shared_lines = _branch_report_lines(lines, request.user.branch_id)
        branch_opening_lines, shared_opening_lines = _branch_report_lines(
            opening_lines,
            request.user.branch_id,
        )
        ledger_rows = _general_ledger_rows(branch_lines, branch_opening_lines)
        shared_ledger_rows = _general_ledger_rows(shared_lines, shared_opening_lines)
    else:
        ledger_rows = _general_ledger_rows(lines, opening_lines)
        shared_ledger_rows = None
    if request.GET.get('format') == 'csv' and not error:
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="general-ledger.csv"'
        writer = csv.writer(response)
        if shared_ledger_rows is None:
            writer.writerow(['Date', 'Reference', 'Account', 'Description', 'Debit', 'Credit', 'Running balance'])
            scoped_rows = [(None, row) for row in ledger_rows]
        else:
            writer.writerow(['Scope', 'Date', 'Reference', 'Account', 'Description', 'Debit', 'Credit', 'Running balance'])
            scoped_rows = (
                [('Assigned branch', row) for row in ledger_rows]
                + [('Shared / unallocated', row) for row in shared_ledger_rows]
            )
        for scope, row in scoped_rows:
            values = [
                row['date'],
                row['reference'],
                row['account'].name,
                row['description'],
                row['debit'],
                row['credit'],
                row['balance'],
            ]
            writer.writerow(
                values if scope is None else [scope, *values]
            )
        return response
    return render(
        request,
        'accounting/ledger.html',
        {
            'organization': organization,
            'accounts': accounts,
            'selected_account': account_id,
            'rows': Paginator(ledger_rows, 50).get_page(request.GET.get('page')),
            'shared_rows': (
                Paginator(shared_ledger_rows, 50).get_page(request.GET.get('shared_page'))
                if shared_ledger_rows is not None
                else None
            ),
            'has_shared_scope': shared_ledger_rows is not None,
            'start': start,
            'end': end,
            'error': error,
            'financial_years': FinancialYear.objects.filter(
                organization=organization
            ).order_by('-starts_on'),
            'selected_financial_year': getattr(request, 'accounting_financial_year', None),
            'active_page': 'accounting',
        },
    )


def _income_statement_rows(lines, account_types):
    aggregates = lines.filter(account__account_type__in=account_types).values(
        'account__code', 'account__name', 'account__account_type'
    ).annotate(debit=Sum('debit'), credit=Sum('credit')).order_by('account__code')
    return _account_summary_rows(aggregates, 'normal')


def _balance_sheet_rows(lines):
    rows = _income_statement_rows(lines, {'asset', 'liability', 'equity'})
    earnings = _income_statement_rows(lines, {'income', 'expense'})
    current_earnings = sum(
        (row['balance'] if row['account_type'] == 'income' else -row['balance'] for row in earnings),
        Decimal('0'),
    )
    if current_earnings:
        rows.append(
            {
                'code': 'CURRENT-EARNINGS',
                'name': 'Current earnings included in equity',
                'debit': max(-current_earnings, Decimal('0')),
                'credit': max(current_earnings, Decimal('0')),
                'balance': current_earnings,
            }
        )
    return rows


@login_required
def profit_and_loss(request):
    organization = _authorized_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    lines, start, end, error = _report_lines(request, organization)
    branch_lines, shared_lines = _branch_report_lines(lines, request.user.branch_id)
    rows = _income_statement_rows(branch_lines, {'income', 'expense'})
    shared_rows = (
        _income_statement_rows(shared_lines, {'income', 'expense'})
        if shared_lines is not None
        else None
    )
    return _render_report(
        request,
        'Profit and loss',
        'accounting/report.html',
        rows,
        organization,
        start,
        end,
        error,
        shared_rows=shared_rows,
    )


@login_required
def balance_sheet(request):
    organization = _authorized_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    lines, start, end, error = _report_lines(request, organization)
    if not error:
        lines = JournalEntryLine.objects.filter(
            entry__organization=organization,
            entry__status='posted',
        ).select_related('account', 'entry')
        selected_year = getattr(request, 'accounting_financial_year', None)
        if selected_year:
            lines = lines.filter(entry__entry_date__gte=selected_year.starts_on)
        else:
            lines = lines.exclude(entry__year_end_link__role='opening')
        if request.user.branch_id:
            lines = lines.filter(Q(entry__branch_id=request.user.branch_id) | Q(entry__branch__isnull=True))
        if end:
            lines = lines.filter(entry__entry_date__lte=end)
    branch_lines, shared_lines = _branch_report_lines(lines, request.user.branch_id)
    rows = _balance_sheet_rows(branch_lines)
    shared_rows = (
        _balance_sheet_rows(shared_lines)
        if shared_lines is not None
        else None
    )
    return _render_report(
        request,
        'Balance sheet',
        'accounting/report.html',
        rows,
        organization,
        None,
        end,
        error,
        note='Balances are cumulative through the end date. Current earnings are included in equity.',
        shared_rows=shared_rows,
    )


@login_required
def accounting_periods(request):
    organization = _authorized_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    can_manage = _can_manage_periods(request.user, organization)
    if request.method == 'POST':
        if not can_manage:
            return HttpResponseForbidden('You do not have permission to manage financial periods.')
        action = request.POST.get('action')
        if action == 'create_year':
            form = FinancialYearForm(request.POST, organization=organization)
            if form.is_valid():
                try:
                    with transaction.atomic():
                        year = form.save(commit=False)
                        year.organization = organization
                        year.save()
                        cursor = year.starts_on
                        while cursor <= year.ends_on:
                            month_end_day = calendar.monthrange(cursor.year, cursor.month)[1]
                            month_end = min(date(cursor.year, cursor.month, month_end_day), year.ends_on)
                            AccountingPeriod.objects.create(
                                financial_year=year,
                                name=cursor.strftime('%B %Y'),
                                starts_on=cursor,
                                ends_on=month_end,
                            )
                            cursor = month_end + timedelta(days=1)
                except ValidationError as exc:
                    form.add_error(None, exc)
                else:
                    messages.success(request, f'Financial year {year.name} and its monthly periods were created.')
                    return redirect('accounting_periods')
        elif action in {'close_period', 'reopen_period'}:
            period = get_object_or_404(
                AccountingPeriod.objects.filter(financial_year__organization=organization),
                pk=request.POST.get('period_id'),
            )
            form = PeriodReopenForm(request.POST) if action == 'reopen_period' else None
            if form is not None and not form.is_valid():
                messages.error(request, '; '.join(
                    message for errors in form.errors.values() for message in errors
                ))
            else:
                try:
                    if action == 'close_period':
                        period.close(user=request.user)
                        messages.success(request, f'{period.name} was closed.')
                    else:
                        period.reopen(user=request.user, reason=form.cleaned_data['reason'])
                        messages.success(request, f'{period.name} was reopened.')
                except ValidationError as exc:
                    messages.error(request, '; '.join(exc.messages))
            return redirect('accounting_periods')
        else:
            return HttpResponseForbidden('Unknown financial-period action.')
    else:
        form = FinancialYearForm(organization=organization)

    years = FinancialYear.objects.filter(organization=organization).prefetch_related(
        'periods__audit_events'
    )
    return render(
        request,
        'accounting/periods.html',
        {
            'organization': organization,
            'years': years,
            'form': form,
            'can_manage': can_manage,
            'reopen_form': PeriodReopenForm(),
            'active_page': 'accounting',
        },
    )


@login_required
def year_end_closing(request, year_id):
    organization = _authorized_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_periods(request.user, organization):
        return HttpResponseForbidden('Year-end closing is available only to organization-level finance users.')
    year = get_object_or_404(
        FinancialYear.objects.filter(organization=organization).select_related(
            'retained_earnings_account'
        ),
        pk=year_id,
    )
    account_qs = Account.objects.filter(
        organization=organization,
        account_type='equity',
        is_active=True,
        branch__isnull=True,
    ).order_by('code')
    if request.method == 'POST':
        form = YearEndCloseForm(request.POST, organization=organization)
        action = request.POST.get('action')
        if action == 'confirm':
            if form.is_valid():
                try:
                    close_financial_year(
                        financial_year_id=year.pk,
                        user=request.user,
                        reason=form.cleaned_data['reason'],
                        retained_earnings_account_id=form.cleaned_data[
                            'retained_earnings_account'
                        ].pk,
                    )
                except ValidationError as exc:
                    for message in exc.messages:
                        form.add_error(None, message)
                else:
                    messages.success(request, f'{year.name} was closed and balances were carried forward.')
                    return redirect('year_end_closing', year_id=year.pk)
        elif action not in {'preview', 'validate'}:
            return HttpResponseForbidden('Unknown year-end closing action.')
        preview_account = form.cleaned_data.get('retained_earnings_account') if form.is_valid() else None
        preview = preview_year_end_close(
            financial_year=year,
            retained_earnings_account=preview_account,
        )
    else:
        form = YearEndCloseForm(
            organization=organization,
            initial={'retained_earnings_account': year.retained_earnings_account_id},
        )
        preview = preview_year_end_close(financial_year=year)

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = (
            f'attachment; filename="year-end-opening-{year.starts_on.isoformat()}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(['Account code', 'Account', 'Branch', 'Type', 'Opening debit', 'Opening credit', 'Net debit balance'])
        branch_names = {
            branch.pk: branch.name
            for branch in organization.branches.all()
        }
        for row in preview['opening_rows']:
            writer.writerow([
                row['code'],
                row['name'],
                branch_names.get(row['branch_id'], 'Organization-wide'),
                row['account_type'],
                row['debit'],
                row['credit'],
                row['balance'],
            ])
        return response
    form.fields['retained_earnings_account'].queryset = account_qs
    return render(
        request,
        'accounting/year_end_closing.html',
        {
            'organization': organization,
            'year': year,
            'form': form,
            'preview': preview,
            'can_confirm': preview['ready'] and preview['existing'] is None,
            'branch_names': {
                branch.pk: branch.name
                for branch in organization.branches.all()
            },
            'active_page': 'accounting',
        },
    )


def _budget_report_rows(budgets):
    budgets = list(budgets)
    if not budgets:
        return []
    period_by_id = {
        budget.period_id: budget.period
        for budget in budgets
    }
    conditions = [
        When(
            entry__entry_date__range=(period.starts_on, period.ends_on),
            then=Value(period_id),
        )
        for period_id, period in period_by_id.items()
    ]
    date_scope = Q()
    for period in period_by_id.values():
        date_scope |= Q(entry__entry_date__range=(period.starts_on, period.ends_on))
    aggregates = (
        JournalEntryLine.objects.filter(
            entry__organization_id=budgets[0].organization_id,
            entry__status='posted',
            account_id__in={budget.account_id for budget in budgets},
        )
        .filter(date_scope)
        .annotate(budget_period_id=Case(*conditions, output_field=IntegerField()))
        .values('account_id', 'entry__branch_id', 'budget_period_id')
        .annotate(debit=Sum('debit'), credit=Sum('credit'))
    )
    actuals = {}
    for aggregate in aggregates:
        actuals.setdefault(
            (aggregate['account_id'], aggregate['budget_period_id']),
            {},
        )[aggregate['entry__branch_id']] = (
            (aggregate['debit'] or Decimal('0')),
            (aggregate['credit'] or Decimal('0')),
        )
    rows = []
    for budget in budgets:
        branch_actuals = actuals.get((budget.account_id, budget.period_id), {})
        if budget.branch_id:
            own_debit, own_credit = branch_actuals.get(
                budget.branch_id,
                (Decimal('0'), Decimal('0')),
            )
            shared_debit, shared_credit = branch_actuals.get(
                None,
                (Decimal('0'), Decimal('0')),
            )
        else:
            totals = list(branch_actuals.values())
            own_debit = sum((values[0] for values in totals), Decimal('0'))
            own_credit = sum((values[1] for values in totals), Decimal('0'))
            shared_debit = Decimal('0')
            shared_credit = Decimal('0')
        is_credit_nature = budget.account.account_type in {'income', 'liability', 'equity'}
        actual = own_credit - own_debit if is_credit_nature else own_debit - own_credit
        unallocated_actual = (
            shared_credit - shared_debit if is_credit_nature else shared_debit - shared_credit
        )
        variance = actual - budget.amount
        rows.append({
            'budget': budget,
            'actual': actual,
            'unallocated_actual': unallocated_actual,
            'variance': variance,
            'variance_percent': (variance * Decimal('100') / budget.amount) if budget.amount else None,
        })
    return rows


@login_required
def budget_report(request):
    organization = _authorized_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    can_manage = _has_financial_role(request.user, organization)
    if request.method == 'POST':
        if not can_manage:
            return HttpResponseForbidden('You do not have permission to manage budgets.')
        form = BudgetForm(
            request.POST,
            organization=organization,
            branch=request.user.branch,
        )
        if form.is_valid():
            budget = form.save(commit=False)
            budget.organization = organization
            budget.created_by = request.user
            try:
                budget.save()
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, 'Budget saved.')
                return redirect('budget_report')
    else:
        form = BudgetForm(organization=organization, branch=request.user.branch)
    budgets = AccountingBudget.objects.filter(organization=organization).select_related(
        'period', 'account', 'branch'
    )
    if request.user.branch_id:
        budgets = budgets.filter(Q(branch_id=request.user.branch_id) | Q(branch__isnull=True))
    page = Paginator(budgets.order_by('-period__starts_on', 'account__code'), 50).get_page(
        request.GET.get('page')
    )
    report_rows = _budget_report_rows(page.object_list)
    if request.GET.get('format') == 'csv':
        if budgets.count() > 10000:
            return HttpResponse('CSV export is limited to 10,000 budget lines.', status=413)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="budget-vs-actual.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Period',
            'Account',
            'Branch',
            'Department',
            'Cost centre',
            'Budget',
            'Actual',
            'Shared / unallocated actual',
            'Variance',
            'Variance %',
        ])
        for row in _budget_report_rows(budgets.order_by('-period__starts_on', 'account__code')):
            budget = row['budget']
            writer.writerow([
                budget.period.name,
                f'{budget.account.code} - {budget.account.name}',
                budget.branch.name if budget.branch_id else '',
                budget.department,
                budget.cost_center,
                budget.amount,
                row['actual'],
                row['unallocated_actual'],
                row['variance'],
                row['variance_percent'] if row['variance_percent'] is not None else '',
            ])
        return response
    return render(
        request,
        'accounting/budgets.html',
        {
            'organization': organization,
            'form': form,
            'rows': page,
            'report_rows': report_rows,
            'can_manage': can_manage,
            'active_page': 'accounting',
        },
    )


@login_required
def fixed_assets(request):
    organization = _authorized_organization(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    can_manage = _has_financial_role(request.user, organization)
    category_form = FixedAssetCategoryForm(
        organization=organization,
        branch=request.user.branch,
    )
    asset_form = FixedAssetForm(
        organization=organization,
        branch=request.user.branch,
    )
    if request.method == 'POST':
        if not can_manage:
            return HttpResponseForbidden('You do not have permission to manage fixed assets.')
        action = request.POST.get('action')
        try:
            if action == 'create_category':
                if request.user.branch_id:
                    return HttpResponseForbidden('Only organization-level finance users can create shared asset categories.')
                category_form = FixedAssetCategoryForm(
                    request.POST,
                    organization=organization,
                    branch=request.user.branch,
                )
                if category_form.is_valid():
                    category = category_form.save(commit=False)
                    category.organization = organization
                    category.save()
                    messages.success(request, 'Fixed-asset category created.')
                    return redirect('fixed_assets')
            elif action == 'register_asset':
                asset_form = FixedAssetForm(
                    request.POST,
                    organization=organization,
                    branch=request.user.branch,
                )
                if asset_form.is_valid():
                    asset = asset_form.save(commit=False)
                    asset.organization = organization
                    if request.user.branch_id:
                        asset.branch = request.user.branch
                    register_fixed_asset(asset=asset, user=request.user)
                    messages.success(request, 'Asset capitalized and recorded.')
                    return redirect('fixed_assets')
            elif action in {'depreciate', 'transfer', 'dispose'}:
                asset_scope = FixedAsset.objects.filter(organization=organization)
                if request.user.branch_id:
                    asset_scope = asset_scope.filter(
                        Q(branch_id=request.user.branch_id) | Q(branch__isnull=True)
                    )
                asset = get_object_or_404(asset_scope, pk=request.POST.get('asset_id'))
                if action == 'depreciate':
                    post_asset_depreciation(
                        asset_id=asset.pk,
                        period_id=request.POST.get('period_id'),
                        user=request.user,
                    )
                    messages.success(request, f'Depreciation recorded for {asset.asset_code}.')
                elif action == 'transfer':
                    if request.user.branch_id:
                        return HttpResponseForbidden('Branch users cannot transfer assets between branches.')
                    form = AssetTransferForm(request.POST, organization=organization)
                    if not form.is_valid():
                        raise ValidationError('Select a valid destination branch.')
                    transfer_fixed_asset(asset_id=asset.pk, branch=form.cleaned_data['branch'], user=request.user)
                    messages.success(request, f'{asset.asset_code} was transferred.')
                else:
                    form = AssetDisposalForm(request.POST)
                    if not form.is_valid():
                        raise ValidationError('Enter a valid disposal date and proceeds amount.')
                    dispose_fixed_asset(
                        asset_id=asset.pk,
                        disposal_date=form.cleaned_data['disposal_date'],
                        proceeds=form.cleaned_data['proceeds'],
                        user=request.user,
                    )
                    messages.success(request, f'{asset.asset_code} was disposed.')
                return redirect('fixed_assets')
            else:
                return HttpResponseForbidden('Unknown fixed-asset action.')
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))

    assets = FixedAsset.objects.filter(organization=organization).select_related(
        'category', 'branch'
    ).prefetch_related(
        'depreciation_records__period',
        'events__user',
    ).annotate(
        accumulated_amount=Coalesce(
            Sum(
                'depreciation_records__amount',
                filter=Q(depreciation_records__journal_entry__status='posted'),
            ),
            Value(Decimal('0')),
            output_field=DecimalField(max_digits=18, decimal_places=2),
        )
    ).annotate(
        calculated_net_book_value=Greatest(
            F('acquisition_cost') - F('accumulated_amount'),
            F('salvage_value'),
        )
    ).order_by('asset_code')
    if request.user.branch_id:
        assets = assets.filter(Q(branch_id=request.user.branch_id) | Q(branch__isnull=True))
    if request.GET.get('format') == 'csv':
        if assets.count() > 10000:
            return HttpResponse('CSV export is limited to 10,000 assets.', status=413)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="fixed-assets.csv"'
        writer = csv.writer(response)
        writer.writerow(['Asset code', 'Name', 'Category', 'Branch', 'Acquired', 'Cost', 'Accumulated depreciation', 'Net book value', 'Disposed'])
        for asset in assets:
            writer.writerow([
                asset.asset_code,
                asset.name,
                asset.category.name,
                asset.branch.name if asset.branch_id else '',
                asset.acquisition_date,
                asset.acquisition_cost,
                asset.accumulated_amount,
                asset.calculated_net_book_value,
                asset.disposed_at or '',
            ])
        return response
    assets_page = Paginator(assets, 50).get_page(request.GET.get('page'))
    open_periods = AccountingPeriod.objects.filter(
        financial_year__organization=organization,
        status='open',
    ).order_by('-starts_on')
    categories = FixedAssetCategory.objects.filter(organization=organization).order_by('name')
    return render(
        request,
        'accounting/fixed_assets.html',
        {
            'organization': organization,
            'category_form': category_form,
            'asset_form': asset_form,
            'categories': categories,
            'assets': assets_page,
            'open_periods': open_periods,
            'transfer_form': AssetTransferForm(organization=organization),
            'disposal_form': AssetDisposalForm(),
            'can_manage': can_manage,
            'can_manage_categories': can_manage and request.user.branch_id is None,
            'can_transfer': can_manage and request.user.branch_id is None,
            'active_page': 'accounting',
        },
    )
