from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from banking.models import BankStatementImport, BankStatementLine, BankTransaction
from purchases.models import PurchaseInvoice
from sales.models import CustomerReceipt, SalesInvoice
from organizations.models import Branch

from .models import (
    Account,
    AccountingPeriod,
    FinancialYear,
    FixedAsset,
    FixedAssetDepreciation,
    FixedAssetEvent,
    JournalEntry,
    JournalEntryLine,
    YearEndClosing,
    YearEndClosingEvent,
    YearEndClosingJournal,
    ensure_default_accounts,
    round_currency,
)


@transaction.atomic
def register_fixed_asset(*, asset, user):
    asset.full_clean()
    asset.save()
    cash = ensure_default_accounts(asset.organization, branch=asset.branch)['cash']
    entry = JournalEntry.objects.create(
        organization=asset.organization,
        branch=asset.branch,
        reference=f'FA-ACQ-{asset.pk}',
        entry_date=asset.acquisition_date,
        narration=f'Capitalization of fixed asset {asset.asset_code}',
        created_by=user,
    )
    JournalEntryLine.objects.create(
        entry=entry,
        account=asset.category.asset_account,
        description=f'Asset capitalization: {asset.name}',
        debit=asset.acquisition_cost,
    )
    JournalEntryLine.objects.create(
        entry=entry,
        account=cash,
        description=f'Asset acquisition payment: {asset.name}',
        credit=asset.acquisition_cost,
    )
    entry.post()
    asset.acquisition_entry = entry
    asset.save(update_fields=['acquisition_entry'])
    FixedAssetEvent.objects.create(
        asset=asset,
        event_type='registered',
        to_branch=asset.branch,
        journal_entry=entry,
        user=user,
        details=f'Capitalized at {asset.acquisition_cost}',
    )
    return asset


@transaction.atomic
def post_asset_depreciation(*, asset_id, period_id, user):
    asset = FixedAsset.objects.select_for_update().select_related(
        'organization', 'branch', 'category'
    ).get(pk=asset_id)
    period = AccountingPeriod.objects.select_for_update().select_related(
        'financial_year'
    ).get(pk=period_id)
    if period.financial_year.organization_id != asset.organization_id:
        raise ValidationError('Depreciation period must belong to the asset organization.')
    existing = FixedAssetDepreciation.objects.filter(asset=asset, period=period).select_related(
        'journal_entry'
    ).first()
    if existing:
        if existing.journal_entry_id and existing.journal_entry.status == 'posted':
            return existing
        raise ValidationError('A depreciation record already exists for this asset and period.')
    if asset.acquisition_entry_id is None or asset.acquisition_entry.status != 'posted':
        raise ValidationError('Only capitalized assets can be depreciated.')
    if asset.disposed_at and period.ends_on >= asset.disposed_at:
        raise ValidationError('Depreciation cannot be posted after the asset disposal date.')
    if period.ends_on < asset.acquisition_date:
        raise ValidationError('Depreciation period ends before the asset was acquired.')
    if period.status != 'open':
        raise ValidationError('Depreciation can only be posted to an open accounting period.')

    depreciable_amount = asset.acquisition_cost - asset.salvage_value
    accumulated = asset.accumulated_depreciation
    remaining = max(depreciable_amount - accumulated, Decimal('0'))
    if asset.category.depreciation_method == 'straight_line':
        monthly = round_currency(depreciable_amount / asset.useful_life_months)
    elif asset.category.depreciation_method == 'reducing_balance':
        monthly = round_currency(
            max(asset.acquisition_cost - accumulated - asset.salvage_value, Decimal('0'))
            * asset.category.annual_rate
            / Decimal('1200')
        )
    else:
        raise ValidationError('Unsupported depreciation method.')
    amount = min(monthly, remaining)
    if amount <= 0:
        raise ValidationError('No depreciable amount remains for this asset.')

    entry = JournalEntry.objects.create(
        organization=asset.organization,
        branch=asset.branch,
        reference=f'FA-DEP-{asset.pk}-{period.pk}',
        entry_date=period.ends_on,
        narration=f'Depreciation for {asset.asset_code} / {period.name}',
        created_by=user,
    )
    JournalEntryLine.objects.create(
        entry=entry,
        account=asset.category.depreciation_expense_account,
        description=f'Depreciation expense: {asset.name}',
        debit=amount,
    )
    JournalEntryLine.objects.create(
        entry=entry,
        account=asset.category.accumulated_depreciation_account,
        description=f'Accumulated depreciation: {asset.name}',
        credit=amount,
    )
    entry.post()
    record = FixedAssetDepreciation.objects.create(
        asset=asset,
        period=period,
        amount=amount,
        journal_entry=entry,
        posted_by=user,
    )
    return record


@transaction.atomic
def transfer_fixed_asset(*, asset_id, branch, user):
    asset = FixedAsset.objects.select_for_update().get(pk=asset_id)
    if asset.disposed_at:
        raise ValidationError('Disposed assets cannot be transferred.')
    if branch is not None and branch.organization_id != asset.organization_id:
        raise ValidationError('Destination branch must belong to the asset organization.')
    previous_branch = asset.branch
    if previous_branch == branch:
        raise ValidationError('Asset is already assigned to that branch.')
    asset.branch = branch
    asset.save(update_fields=['branch'])
    FixedAssetEvent.objects.create(
        asset=asset,
        event_type='transferred',
        from_branch=previous_branch,
        to_branch=branch,
        user=user,
        details=f'Transferred from {previous_branch or "organization"} to {branch or "organization"}',
    )
    return asset


@transaction.atomic
def dispose_fixed_asset(*, asset_id, disposal_date, proceeds, user):
    asset = FixedAsset.objects.select_for_update().select_related(
        'organization', 'branch', 'category'
    ).get(pk=asset_id)
    if asset.disposed_at:
        raise ValidationError('This asset has already been disposed.')
    if asset.acquisition_entry_id is None or asset.acquisition_entry.status != 'posted':
        raise ValidationError('Only capitalized assets can be disposed.')
    if disposal_date < asset.acquisition_date:
        raise ValidationError('Disposal date cannot be before acquisition.')
    proceeds = Decimal(proceeds)
    if not proceeds.is_finite() or proceeds < 0 or proceeds.as_tuple().exponent < -2:
        raise ValidationError('Disposal proceeds must be a nonnegative currency amount.')

    accumulated = asset.accumulated_depreciation
    book_value = max(asset.acquisition_cost - accumulated, Decimal('0'))
    gain = max(proceeds - book_value, Decimal('0'))
    loss = max(book_value - proceeds, Decimal('0'))
    accounts = ensure_default_accounts(asset.organization, branch=asset.branch)
    entry = JournalEntry.objects.create(
        organization=asset.organization,
        branch=asset.branch,
        reference=f'FA-DISP-{asset.pk}',
        entry_date=disposal_date,
        narration=f'Disposal of fixed asset {asset.asset_code}',
        created_by=user,
    )
    if proceeds:
        JournalEntryLine.objects.create(
            entry=entry,
            account=accounts['cash'],
            description='Disposal proceeds',
            debit=proceeds,
        )
    if accumulated:
        JournalEntryLine.objects.create(
            entry=entry,
            account=asset.category.accumulated_depreciation_account,
            description='Remove accumulated depreciation',
            debit=accumulated,
        )
    JournalEntryLine.objects.create(
        entry=entry,
        account=asset.category.asset_account,
        description='Remove asset acquisition cost',
        credit=asset.acquisition_cost,
    )
    if gain:
        JournalEntryLine.objects.create(
            entry=entry,
            account=accounts['fixed_asset_disposal_gain'],
            description='Gain on fixed-asset disposal',
            credit=gain,
        )
    if loss:
        JournalEntryLine.objects.create(
            entry=entry,
            account=accounts['fixed_asset_disposal_loss'],
            description='Loss on fixed-asset disposal',
            debit=loss,
        )
    entry.post()
    asset.disposed_at = disposal_date
    asset.disposal_entry = entry
    asset.disposal_proceeds = proceeds
    asset.save(update_fields=['disposed_at', 'disposal_entry', 'disposal_proceeds'])
    FixedAssetEvent.objects.create(
        asset=asset,
        event_type='disposed',
        from_branch=asset.branch,
        journal_entry=entry,
        user=user,
        details=f'Disposed for {proceeds}; gain {gain}; loss {loss}',
    )
    return asset


def _year_end_period_checklist(year):
    periods = list(year.periods.order_by('starts_on'))
    errors = []
    checks = []
    cursor = year.starts_on
    coverage_valid = bool(periods)
    for period in periods:
        if period.starts_on != cursor:
            coverage_valid = False
            break
        cursor = period.ends_on.fromordinal(period.ends_on.toordinal() + 1)
    if cursor != year.ends_on.fromordinal(year.ends_on.toordinal() + 1):
        coverage_valid = False
    checks.append({
        'label': 'Accounting periods cover the full financial year',
        'passed': coverage_valid,
        'detail': 'All dates are covered without gaps.' if coverage_valid else
            'Create periods that cover the complete financial year without gaps.',
    })
    if not coverage_valid:
        errors.append(checks[-1]['detail'])
    all_closed = bool(periods) and all(period.status == 'closed' for period in periods)
    checks.append({
        'label': 'All accounting periods are closed',
        'passed': all_closed,
        'detail': 'All periods are closed.' if all_closed else
            'Close every accounting period in this financial year before year-end closing.',
    })
    if not all_closed:
        errors.append(checks[-1]['detail'])
    return periods, checks, errors


def _year_end_balance_plan(year, retained_earnings_account):
    organization_id = year.organization_id
    pnl = list(
        JournalEntryLine.objects.filter(
            entry__organization_id=organization_id,
            entry__status='posted',
            entry__entry_date__range=(year.starts_on, year.ends_on),
            account__account_type__in=('income', 'expense'),
        )
        .values(
            'account_id',
            'account__code',
            'account__name',
            'account__account_type',
            'account__is_active',
            'account__organization_id',
            'account__branch_id',
            'entry__branch_id',
        )
        .annotate(debit=Sum('debit'), credit=Sum('credit'))
        .order_by('entry__branch_id', 'account__code')
    )
    closing_by_branch = {}
    profit_by_branch = {}
    errors = []
    pnl_rows = []
    for item in pnl:
        net = (item['debit'] or Decimal('0')) - (item['credit'] or Decimal('0'))
        if not net:
            continue
        branch_id = item['entry__branch_id']
        if not item['account__is_active']:
            errors.append(
                f"Reactivate account {item['account__code']} ({item['account__name']}) "
                'before closing this financial year.'
            )
        closing_by_branch.setdefault(branch_id, []).append({
            'account_id': item['account_id'],
            'code': item['account__code'],
            'name': item['account__name'],
            'debit': max(-net, Decimal('0')),
            'credit': max(net, Decimal('0')),
        })
        profit_by_branch[branch_id] = profit_by_branch.get(branch_id, Decimal('0')) + net
        pnl_rows.append({
            'branch_id': branch_id,
            'code': item['account__code'],
            'name': item['account__name'],
            'account_type': item['account__account_type'],
            'debit': item['debit'] or Decimal('0'),
            'credit': item['credit'] or Decimal('0'),
            'net': net,
        })
    for branch_id, net in profit_by_branch.items():
        if retained_earnings_account is not None and net:
            closing_by_branch[branch_id].append({
                'account_id': retained_earnings_account.pk,
                'code': retained_earnings_account.code,
                'name': retained_earnings_account.name,
                'debit': max(net, Decimal('0')),
                'credit': max(-net, Decimal('0')),
            })

    prior_pnl = (
        JournalEntryLine.objects.filter(
            entry__organization_id=organization_id,
            entry__status='posted',
            entry__entry_date__lt=year.starts_on,
            account__account_type__in=('income', 'expense'),
        ).exclude(entry__year_end_link__role='opening')
        .values('account_id', 'account__code', 'entry__branch_id')
        .annotate(debit=Sum('debit'), credit=Sum('credit'))
    )
    unclosed_prior_pnl = [
        item for item in prior_pnl
        if (item['debit'] or Decimal('0')) != (item['credit'] or Decimal('0'))
    ]
    if unclosed_prior_pnl:
        errors.append(
            'Earlier income or expense balances remain unclosed. Complete prior-year closes '
            'or post an approved correction before closing this year.'
        )

    balance_sheet = list(
        JournalEntryLine.objects.filter(
            entry__organization_id=organization_id,
            entry__status='posted',
            entry__entry_date__lte=year.ends_on,
            account__account_type__in=('asset', 'liability', 'equity'),
        ).exclude(entry__year_end_link__role='opening')
        .values(
            'account_id',
            'account__code',
            'account__name',
            'account__account_type',
            'account__is_active',
            'account__branch_id',
            'entry__branch_id',
        )
        .annotate(debit=Sum('debit'), credit=Sum('credit'))
        .order_by('entry__branch_id', 'account__code')
    )
    opening_by_branch = {}
    for item in balance_sheet:
        net = (item['debit'] or Decimal('0')) - (item['credit'] or Decimal('0'))
        if not net:
            continue
        branch_id = item['entry__branch_id']
        opening_by_branch.setdefault(branch_id, {})
        account_row = opening_by_branch[branch_id].setdefault(
            item['account_id'],
            {
                'account_id': item['account_id'],
                'code': item['account__code'],
                'name': item['account__name'],
                'account_type': item['account__account_type'],
                'net': Decimal('0'),
                'active': item['account__is_active'],
                'account_branch_id': item['account__branch_id'],
            },
        )
        account_row['net'] += net
    for branch_id, net in profit_by_branch.items():
        if net:
            if retained_earnings_account is None:
                errors.append('Configure an active organization-wide retained earnings equity account.')
            else:
                retained_row = opening_by_branch.setdefault(branch_id, {}).setdefault(
                    retained_earnings_account.pk,
                    {
                        'account_id': retained_earnings_account.pk,
                        'code': retained_earnings_account.code,
                        'name': retained_earnings_account.name,
                        'account_type': 'equity',
                        'net': Decimal('0'),
                        'active': retained_earnings_account.is_active,
                        'account_branch_id': retained_earnings_account.branch_id,
                    },
                )
                retained_row['net'] += net

    opening_rows = []
    for branch_id, accounts in opening_by_branch.items():
        branch_net = Decimal('0')
        for account_row in accounts.values():
            net = account_row['net']
            if not net:
                continue
            branch_net += net
            if not account_row['active']:
                errors.append(
                    f"Reactivate balance-sheet account {account_row['code']} "
                    f"({account_row['name']}) before carrying it forward."
                )
            if account_row['account_branch_id'] and account_row['account_branch_id'] != branch_id:
                errors.append(
                    f"Account {account_row['code']} has a branch assignment that conflicts "
                    'with its posted ledger activity.'
                )
            opening_rows.append({
                'branch_id': branch_id,
                'account_id': account_row['account_id'],
                'code': account_row['code'],
                'name': account_row['name'],
                'account_type': account_row['account_type'],
                'debit': max(net, Decimal('0')),
                'credit': max(-net, Decimal('0')),
                'balance': net,
            })
        if branch_net:
            errors.append(
                f'Opening balances for branch {branch_id or "organization-wide"} do not balance '
                f'(net debit {round_currency(branch_net)}). Resolve the ledger imbalance before closing.'
            )

    return {
        'closing_by_branch': closing_by_branch,
        'opening_by_branch': opening_by_branch,
        'opening_rows': opening_rows,
        'pnl_rows': pnl_rows,
        'profit': round_currency(-sum(profit_by_branch.values(), Decimal('0'))),
        'profit_by_branch': profit_by_branch,
        'errors': list(dict.fromkeys(errors)),
    }


def preview_year_end_close(*, financial_year, retained_earnings_account=None):
    year = FinancialYear.objects.select_related('organization').get(pk=financial_year.pk)
    errors = []
    checks = []
    periods, period_checks, period_errors = _year_end_period_checklist(year)
    checks.extend(period_checks)
    errors.extend(period_errors)

    draft_journals = JournalEntry.objects.filter(
        organization=year.organization,
        status='draft',
        entry_date__range=(year.starts_on, year.ends_on),
    ).count()
    draft_sales = SalesInvoice.objects.filter(
        organization=year.organization,
        status='draft',
        invoice_date__range=(year.starts_on, year.ends_on),
    ).count()
    draft_purchases = PurchaseInvoice.objects.filter(
        organization=year.organization,
        status='draft',
        invoice_date__range=(year.starts_on, year.ends_on),
    ).count()
    draft_receipts = CustomerReceipt.objects.filter(
        organization=year.organization,
        status='draft',
        receipt_date__range=(year.starts_on, year.ends_on),
    ).count()
    draft_bank_transactions = BankTransaction.objects.filter(
        organization=year.organization,
        status='draft',
        transaction_date__range=(year.starts_on, year.ends_on),
    ).count()
    pending_statement_imports = BankStatementImport.objects.filter(
        organization=year.organization,
        status='pending',
    ).count()
    pending = []
    for label, count in (
        ('draft journal entries', draft_journals),
        ('draft sales invoices', draft_sales),
        ('draft purchase invoices', draft_purchases),
        ('draft customer receipts', draft_receipts),
        ('draft bank transactions', draft_bank_transactions),
        ('pending bank statement imports', pending_statement_imports),
    ):
        if count:
            pending.append(f'{count} {label}')
    checks.append({
        'label': 'No unposted transactions remain',
        'passed': not pending,
        'detail': 'No draft financial transactions found.' if not pending else ', '.join(pending) + '.',
    })
    if pending:
        errors.append('Post or resolve these drafts before closing: ' + ', '.join(pending) + '.')

    unmatched = BankStatementLine.objects.filter(
        import_record__organization=year.organization,
        status='unmatched',
        statement_date__range=(year.starts_on, year.ends_on),
    ).count()
    checks.append({
        'label': 'No unmatched bank statement lines',
        'passed': unmatched == 0,
        'detail': 'No unmatched statement lines.' if not unmatched else f'{unmatched} unmatched statement line(s).',
    })
    if unmatched:
        errors.append(f'Reconcile or otherwise resolve {unmatched} unmatched bank statement line(s).')

    posted_entries = JournalEntry.objects.filter(
        organization=year.organization,
        status='posted',
        entry_date__lte=year.ends_on,
    )
    journal_totals = list(
        posted_entries.values('id', 'reference')
        .annotate(debit_total=Sum('lines__debit'), credit_total=Sum('lines__credit'))
    )
    unbalanced_entries = [
        item for item in journal_totals
        if (item['debit_total'] or Decimal('0')) != (item['credit_total'] or Decimal('0'))
    ]
    if unbalanced_entries:
        errors.append(
            'Unbalanced posted journals require correction: ' +
            ', '.join(item['reference'] for item in unbalanced_entries[:10]) + '.'
        )
    invalid_lines = []
    for line in JournalEntryLine.objects.filter(entry__in=posted_entries).select_related(
        'entry__branch', 'account'
    ):
        if (
            line.account.organization_id != year.organization_id
            or (line.entry.branch_id and line.entry.branch.organization_id != year.organization_id)
            or (line.account.branch_id and line.account.branch_id != line.entry.branch_id)
        ):
            invalid_lines.append(f'{line.entry.reference}:{line.account.code}')
    if invalid_lines:
        errors.append(
            'Journal lines with organization/branch mismatches require correction: ' +
            ', '.join(invalid_lines[:10]) + '.'
        )
    checks.append({
        'label': 'Posted journals are balanced and tenant/branch-consistent',
        'passed': not unbalanced_entries and not invalid_lines,
        'detail': 'All posted journals passed validation.' if not unbalanced_entries and not invalid_lines else
            'See validation errors for affected journal references.',
    })

    trial_balance = JournalEntryLine.objects.filter(
        entry__organization=year.organization,
        entry__status='posted',
        entry__entry_date__lte=year.ends_on,
    ).exclude(entry__year_end_link__role='opening').aggregate(
        debit=Sum('debit'),
        credit=Sum('credit'),
    )
    trial_debit = trial_balance['debit'] or Decimal('0')
    trial_credit = trial_balance['credit'] or Decimal('0')
    trial_balanced = trial_debit == trial_credit
    checks.append({
        'label': 'Cumulative trial balance is balanced',
        'passed': trial_balanced,
        'detail': f'Debits {round_currency(trial_debit)}; credits {round_currency(trial_credit)}.',
    })
    if not trial_balanced:
        errors.append(
            f'Cumulative trial balance does not balance (debits {round_currency(trial_debit)}, '
            f'credits {round_currency(trial_credit)}).'
        )

    retained = retained_earnings_account or year.retained_earnings_account
    retained_valid = bool(
        retained
        and retained.organization_id == year.organization_id
        and retained.account_type == 'equity'
        and retained.is_active
        and retained.branch_id is None
    )
    checks.append({
        'label': 'Retained earnings account is configured',
        'passed': retained_valid,
        'detail': 'An active organization-wide equity account is configured.' if retained_valid else
            'Select an active organization-wide equity account for retained earnings.',
    })
    if not retained_valid:
        errors.append('Configure an active organization-wide retained earnings equity account.')

    next_year = FinancialYear.objects.filter(
        organization=year.organization,
        starts_on=year.ends_on.fromordinal(year.ends_on.toordinal() + 1),
    ).first()
    next_period = None
    if next_year:
        next_period = next_year.periods.filter(
            starts_on__lte=next_year.starts_on,
            ends_on__gte=next_year.starts_on,
        ).first()
    next_ready = bool(next_year and next_period and next_period.status == 'open')
    checks.append({
        'label': 'Next financial year and opening period are ready',
        'passed': next_ready,
        'detail': f'{next_year.name} / {next_period.name} is open.' if next_ready else
            'Create the immediately following financial year with an open first accounting period.',
    })
    if not next_ready:
        errors.append(
            'Create the immediately following financial year (starting the day after this year ends) '
            'and an open accounting period covering its first date.'
        )

    existing = YearEndClosing.objects.filter(financial_year=year).first()
    if existing and existing.status == 'closed':
        checks.append({
            'label': 'Year has not already been finalized',
            'passed': False,
            'detail': 'This year is already closed; the operation is immutable.',
        })
    else:
        checks.append({
            'label': 'Year has not already been finalized',
            'passed': True,
            'detail': 'No completed close exists.',
        })

    plan = _year_end_balance_plan(year, retained if retained_valid else None)
    errors.extend(plan['errors'])
    branch_names = {
        branch.pk: branch.name
        for branch in Branch.objects.filter(organization=year.organization)
    }
    for row in plan['pnl_rows']:
        row['branch_name'] = branch_names.get(row['branch_id'], 'Organization-wide')
    for row in plan['opening_rows']:
        row['branch_name'] = branch_names.get(row['branch_id'], 'Organization-wide')
    checks.append({
        'label': 'Opening balance groups balance and use active accounts',
        'passed': not plan['errors'],
        'detail': 'Proposed branch and organization opening balances balance.' if not plan['errors'] else
            'Review the detailed year-end calculation errors.',
    })
    errors = list(dict.fromkeys(errors))
    existing_journals = []
    if existing:
        existing_journals = list(
            existing.journals.select_related('journal_entry', 'branch').prefetch_related(
                'journal_entry__lines__account'
            )
        )
    return {
        'financial_year': year,
        'periods': periods,
        'next_year': next_year,
        'retained_earnings_account': retained,
        'checks': checks,
        'errors': errors,
        'ready': not errors and not (existing and existing.status == 'closed'),
        'trial_debit': round_currency(trial_debit),
        'trial_credit': round_currency(trial_credit),
        'profit': existing.profit_loss if existing and existing.status == 'closed' else plan['profit'],
        'profit_by_branch': plan['profit_by_branch'],
        'pnl_rows': plan['pnl_rows'],
        'opening_rows': plan['opening_rows'],
        'branch_names': branch_names,
        'existing': existing,
        'existing_journals': existing_journals,
    }


@transaction.atomic
def close_financial_year(*, financial_year_id, user, reason, retained_earnings_account_id):
    if not reason or not reason.strip():
        raise ValidationError('A reason is required to close the financial year.')
    year = FinancialYear.objects.select_for_update().select_related('organization').get(
        pk=financial_year_id
    )
    authorized = user.branch_id is None and (
        user.is_superuser
        or user.organizations_memberships.filter(
            organization=year.organization,
            is_active=True,
            role__in={'owner', 'admin', 'accountant', 'finance_manager'},
        ).exists()
    )
    if not authorized:
        raise ValidationError('Only organization-level finance users can close a financial year.')
    prior_close = YearEndClosing.objects.filter(financial_year=year).first()
    if prior_close and prior_close.status == 'closed':
        return prior_close
    retained = Account.objects.select_for_update().filter(
        pk=retained_earnings_account_id,
        organization=year.organization,
        account_type='equity',
        is_active=True,
        branch__isnull=True,
    ).first()
    if retained is None:
        raise ValidationError('Select an active organization-wide equity account for retained earnings.')
    list(
        AccountingPeriod.objects.select_for_update().filter(
            financial_year=year
        ).order_by('starts_on')
    )
    existing, created = YearEndClosing.objects.get_or_create(
        financial_year=year,
        defaults={
            'retained_earnings_account': retained,
            'status': 'closing',
            'reason': reason.strip()[:255],
        },
    )
    if not created:
        if existing.status == 'closed':
            return existing
        raise ValidationError('A year-end close is already in progress for this financial year.')

    preview = preview_year_end_close(
        financial_year=year,
        retained_earnings_account=retained,
    )
    if not preview['ready']:
        raise ValidationError(preview['errors'])
    year.retained_earnings_account = retained
    year.save(update_fields=['retained_earnings_account'])

    plan = _year_end_balance_plan(year, retained)
    organization = year.organization
    for branch_id, lines in plan['closing_by_branch'].items():
        if not lines:
            continue
        branch = None
        if branch_id:
            from organizations.models import Branch

            branch = Branch.objects.get(pk=branch_id, organization=organization)
        suffix = f'B{branch_id}' if branch_id else 'ORG'
        entry = JournalEntry.objects.create(
            organization=organization,
            branch=branch,
            reference=f'YE-CLOSE-{year.pk}-{suffix}',
            entry_date=year.ends_on,
            narration=f'Year-end income and expense close for {year.name}',
            created_by=user,
        )
        for item in lines:
            JournalEntryLine.objects.create(
                entry=entry,
                account_id=item['account_id'],
                description=f'Year-end close: {item["code"]} {item["name"]}'[:200],
                debit=item['debit'],
                credit=item['credit'],
            )
        YearEndClosingJournal.objects.create(
            closing=existing,
            journal_entry=entry,
            role='closing',
            branch=branch,
        )
        entry.post_year_end_close(closing=existing)

    next_year = preview['next_year']
    for branch_id, accounts in plan['opening_by_branch'].items():
        lines = [item for item in accounts.values() if item['net']]
        if not lines:
            continue
        branch = None
        if branch_id:
            from organizations.models import Branch

            branch = Branch.objects.get(pk=branch_id, organization=organization)
        suffix = f'B{branch_id}' if branch_id else 'ORG'
        entry = JournalEntry.objects.create(
            organization=organization,
            branch=branch,
            reference=f'YE-OPEN-{year.pk}-{suffix}',
            entry_date=next_year.starts_on,
            narration=f'Opening balances carried forward from {year.name}',
            created_by=user,
        )
        for item in lines:
            net = item['net']
            JournalEntryLine.objects.create(
                entry=entry,
                account_id=item['account_id'],
                description=f'Opening balance: {item["code"]} {item["name"]}'[:200],
                debit=max(net, Decimal('0')),
                credit=max(-net, Decimal('0')),
            )
        YearEndClosingJournal.objects.create(
            closing=existing,
            journal_entry=entry,
            role='opening',
            branch=branch,
        )
        entry.post_year_end_opening(closing=existing)

    references = ', '.join(
        existing.journals.order_by('role', 'branch_id').values_list('journal_entry__reference', flat=True)
    )
    YearEndClosingEvent.objects.create(
        closing=existing,
        action='closed',
        user=user,
        reason=reason.strip()[:255],
        journal_references=references,
    )
    existing.finalize(
        profit_loss=plan['profit'],
        closed_by=user,
        closed_at=timezone.now(),
    )
    return existing
