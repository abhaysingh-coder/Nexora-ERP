from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse

from core.models import User
from organizations.models import Branch, Organization, OrganizationMembership
from banking.models import BankAccount, BankTransaction
from purchases.models import PurchaseInvoice, Supplier
from sales.models import Customer, CustomerReceipt, SalesInvoice

from .models import (
    Account,
    AccountingBudget,
    AccountingPeriod,
    FinancialYear,
    FixedAsset,
    FixedAssetCategory,
    PeriodAuditEvent,
    JournalEntry,
    JournalEntryLine,
    YearEndClosing,
    YearEndClosingEvent,
    YearEndClosingJournal,
    ensure_default_accounts,
    round_currency,
)
from .services import (
    close_financial_year,
    dispose_fixed_asset,
    post_asset_depreciation,
    preview_year_end_close,
    register_fixed_asset,
    transfer_fixed_asset,
)


class AccountingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Ledger Co', slug='ledger-co')
        self.branch = Branch.objects.create(organization=self.organization, name='Main', code='MAIN')
        self.cash = Account.objects.create(
            organization=self.organization,
            branch=self.branch,
            code='1000',
            name='Cash',
            account_type='asset',
        )
        self.sales = Account.objects.create(
            organization=self.organization,
            branch=self.branch,
            code='4000',
            name='Sales',
            account_type='income',
        )
        self.user = User.objects.create_user(
            username='accountant',
            email='accountant@example.test',
            password='test-password',
            organization=self.organization,
            branch=self.branch,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='accountant',
        )

    def test_currency_rounding_uses_half_up_to_two_decimal_places(self):
        self.assertEqual(round_currency(Decimal('1.005')), Decimal('1.01'))
        self.assertEqual(round_currency(Decimal('1.004')), Decimal('1.00'))

    def test_closed_accounting_period_blocks_posting_and_reopening_is_audited(self):
        year = FinancialYear.objects.create(
            organization=self.organization,
            name='FY 2026',
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        period = AccountingPeriod.objects.create(
            financial_year=year,
            name='May 2026',
            starts_on=date(2026, 5, 1),
            ends_on=date(2026, 5, 31),
        )
        period.close(user=self.user)
        entry = self._entry('PERIOD-LOCKED')
        with self.assertRaises(ValidationError):
            entry.post()
        period.reopen(user=self.user, reason='Approved correction')
        entry.post()
        self.assertEqual(period.audit_events.count(), 2)
        self.assertEqual(period.audit_events.filter(action='reopened').get().reason, 'Approved correction')

    def test_period_cannot_close_with_draft_journal(self):
        year = FinancialYear.objects.create(
            organization=self.organization,
            name='FY 2027',
            starts_on=date(2027, 1, 1),
            ends_on=date(2027, 12, 31),
        )
        period = AccountingPeriod.objects.create(
            financial_year=year,
            name='January 2027',
            starts_on=date(2027, 1, 1),
            ends_on=date(2027, 1, 31),
        )
        JournalEntry.objects.create(
            organization=self.organization,
            branch=self.branch,
            reference='DRAFT-PERIOD',
            entry_date=date(2027, 1, 15),
        )
        with self.assertRaises(ValidationError):
            period.close(user=self.user)
        period.refresh_from_db()
        self.assertEqual(period.status, 'open')

    def test_financial_year_page_generates_monthly_periods(self):
        self.client.force_login(self._organization_finance_user('period-manager'))
        response = self.client.post(
            reverse('accounting_periods'),
            {
                'action': 'create_year',
                'name': 'FY 2026-27',
                'starts_on': '2026-04-01',
                'ends_on': '2027-03-31',
            },
        )
        self.assertEqual(response.status_code, 302)
        year = FinancialYear.objects.get(name='FY 2026-27')
        self.assertEqual(year.periods.count(), 12)

    def test_budget_report_compares_posted_actuals_with_budget(self):
        year = FinancialYear.objects.create(
            organization=self.organization,
            name='FY Budget 2026',
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        period = AccountingPeriod.objects.create(
            financial_year=year,
            name='May 2026',
            starts_on=date(2026, 5, 1),
            ends_on=date(2026, 5, 31),
        )
        expense = Account.objects.create(
            organization=self.organization,
            code='5000',
            name='Office expense',
            account_type='expense',
        )
        entry = JournalEntry.objects.create(
            organization=self.organization,
            reference='BUDGET-ACTUAL',
            entry_date=date(2026, 5, 10),
        )
        cash = Account.objects.create(
            organization=self.organization,
            code='1001',
            name='Cash budget test',
            account_type='asset',
        )
        JournalEntryLine.objects.create(entry=entry, account=expense, debit=Decimal('25'))
        JournalEntryLine.objects.create(entry=entry, account=cash, credit=Decimal('25'))
        entry.post()
        AccountingBudget.objects.create(
            organization=self.organization,
            period=period,
            account=expense,
            amount=Decimal('100'),
            created_by=self.user,
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('budget_report'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '25')
        self.assertContains(response, '-75')

    def test_fixed_asset_capitalization_depreciation_idempotency_and_disposal(self):
        year = FinancialYear.objects.create(
            organization=self.organization,
            name='FY Assets 2026',
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        may = AccountingPeriod.objects.create(
            financial_year=year,
            name='May 2026',
            starts_on=date(2026, 5, 1),
            ends_on=date(2026, 5, 31),
        )
        june = AccountingPeriod.objects.create(
            financial_year=year,
            name='June 2026',
            starts_on=date(2026, 6, 1),
            ends_on=date(2026, 6, 30),
        )
        asset_account = Account.objects.create(
            organization=self.organization,
            code='1500',
            name='Equipment',
            account_type='asset',
        )
        depreciation_account = Account.objects.create(
            organization=self.organization,
            code='1590',
            name='Accumulated depreciation',
            account_type='asset',
        )
        expense_account = Account.objects.create(
            organization=self.organization,
            code='5800',
            name='Depreciation expense',
            account_type='expense',
        )
        category = FixedAssetCategory.objects.create(
            organization=self.organization,
            name='Equipment',
            asset_account=asset_account,
            accumulated_depreciation_account=depreciation_account,
            depreciation_expense_account=expense_account,
            default_useful_life_months=12,
        )
        asset = register_fixed_asset(
            asset=FixedAsset(
                organization=self.organization,
                branch=self.branch,
                category=category,
                asset_code='ASSET-1',
                name='Office printer',
                acquisition_date=date(2026, 5, 10),
                acquisition_cost=Decimal('12000'),
                salvage_value=Decimal('1200'),
                useful_life_months=12,
            ),
            user=self.user,
        )
        depreciation = post_asset_depreciation(
            asset_id=asset.pk,
            period_id=may.pk,
            user=self.user,
        )
        repeated = post_asset_depreciation(
            asset_id=asset.pk,
            period_id=may.pk,
            user=self.user,
        )
        self.assertEqual(depreciation.pk, repeated.pk)
        self.assertEqual(depreciation.amount, Decimal('900.00'))
        self.assertEqual(asset.accumulated_depreciation, Decimal('900.00'))

        transfer_fixed_asset(asset_id=asset.pk, branch=None, user=self.user)
        disposed = dispose_fixed_asset(
            asset_id=asset.pk,
            disposal_date=date(2026, 6, 15),
            proceeds=Decimal('11000'),
            user=self.user,
        )
        self.assertEqual(disposed.disposed_at, date(2026, 6, 15))
        self.assertEqual(disposed.disposal_entry.status, 'posted')
        self.assertTrue(disposed.events.filter(event_type='transferred').exists())
        self.assertTrue(disposed.events.filter(event_type='disposed').exists())
        self.assertEqual(disposed.disposal_entry.total_debit, disposed.disposal_entry.total_credit)

    def test_fixed_asset_ui_creates_category_and_capitalizes_asset(self):
        asset_account = Account.objects.create(
            organization=self.organization,
            code='1510',
            name='Vehicles',
            account_type='asset',
        )
        depreciation_account = Account.objects.create(
            organization=self.organization,
            code='1591',
            name='Vehicle depreciation',
            account_type='asset',
        )
        expense_account = Account.objects.create(
            organization=self.organization,
            code='5810',
            name='Vehicle depreciation expense',
            account_type='expense',
        )
        self.client.force_login(self._organization_finance_user('asset-manager'))
        page = self.client.get(reverse('fixed_assets'))
        self.assertEqual(page.status_code, 200)
        response = self.client.post(
            reverse('fixed_assets'),
            {
                'action': 'create_category',
                'name': 'Vehicles',
                'asset_account': asset_account.pk,
                'accumulated_depreciation_account': depreciation_account.pk,
                'depreciation_expense_account': expense_account.pk,
                'depreciation_method': 'straight_line',
                'annual_rate': '0',
                'default_useful_life_months': '48',
            },
        )
        self.assertEqual(response.status_code, 302)
        category = FixedAssetCategory.objects.get(name='Vehicles')
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('fixed_assets'),
            {
                'action': 'register_asset',
                'category': category.pk,
                'asset_code': 'VEH-1',
                'name': 'Delivery van',
                'acquisition_date': '2026-05-10',
                'acquisition_cost': '24000.00',
                'salvage_value': '1000.00',
                'useful_life_months': '48',
            },
        )
        self.assertEqual(response.status_code, 302)
        asset = FixedAsset.objects.get(asset_code='VEH-1')
        self.assertEqual(asset.branch_id, self.branch.pk)
        self.assertEqual(asset.acquisition_entry.status, 'posted')

    def test_branch_finance_user_can_manage_assets_but_not_shared_categories(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse('fixed_assets'))
        self.assertTrue(page.context['can_manage'])
        self.assertFalse(page.context['can_manage_categories'])
        response = self.client.post(
            reverse('fixed_assets'),
            {'action': 'create_category', 'name': 'Restricted'},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FixedAssetCategory.objects.filter(name='Restricted').exists())

    def _entry(self, reference='SALE-1'):
        entry = JournalEntry.objects.create(
            organization=self.organization,
            branch=self.branch,
            reference=reference,
            entry_date=date(2026, 5, 1),
        )
        JournalEntryLine.objects.create(entry=entry, account=self.cash, debit=Decimal('100'))
        JournalEntryLine.objects.create(entry=entry, account=self.sales, credit=Decimal('100'))
        return entry

    def _organization_finance_user(self, username):
        user = User.objects.create_user(
            username=username,
            email=f'{username}@example.test',
            password='test-password',
            organization=self.organization,
        )
        OrganizationMembership.objects.create(
            user=user,
            organization=self.organization,
            role='accountant',
        )
        return user

    def test_journal_posting_is_balanced_idempotent_and_lines_immutable(self):
        entry = self._entry()
        entry.post()
        entry.post()
        self.cash.refresh_from_db()
        self.sales.refresh_from_db()
        self.assertEqual(self.cash.current_balance, Decimal('100'))
        self.assertEqual(self.sales.current_balance, Decimal('-100'))
        with self.assertRaises(ValidationError):
            entry.lines.first().delete()

    def test_journal_rejects_empty_unbalanced_and_cross_organization_entries(self):
        empty = JournalEntry.objects.create(
            organization=self.organization,
            reference='EMPTY',
            entry_date=date(2026, 5, 1),
        )
        with self.assertRaises(ValidationError):
            empty.post()

        unbalanced = JournalEntry.objects.create(
            organization=self.organization,
            branch=self.branch,
            reference='UNBALANCED',
            entry_date=date(2026, 5, 1),
        )
        JournalEntryLine.objects.create(entry=unbalanced, account=self.cash, debit=Decimal('10'))
        with self.assertRaises(ValidationError):
            unbalanced.post()

        other = Organization.objects.create(name='Other Ledger', slug='other-ledger')
        foreign_account = Account.objects.create(
            organization=other,
            code='1000',
            name='Foreign',
            account_type='asset',
        )
        bad = JournalEntry.objects.create(
            organization=self.organization,
            reference='CROSS-ORG',
            entry_date=date(2026, 5, 1),
        )
        with self.assertRaises(ValidationError):
            JournalEntryLine.objects.create(entry=bad, account=foreign_account, debit=Decimal('1'))

    def test_database_rejects_journal_lines_with_both_sides_or_no_amount(self):
        entry = JournalEntry.objects.create(
            organization=self.organization,
            branch=self.branch,
            reference='INVALID-LINE',
            entry_date=date(2026, 5, 1),
        )
        for debit, credit in ((Decimal('1'), Decimal('1')), (Decimal('0'), Decimal('0'))):
            with self.subTest(debit=debit, credit=credit):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        JournalEntryLine.objects.bulk_create(
                            [JournalEntryLine(entry=entry, account=self.cash, debit=debit, credit=credit)]
                        )

    def test_default_chart_accounts_remain_shared_across_branches(self):
        second_branch = Branch.objects.create(
            organization=self.organization,
            name='Warehouse',
            code='WH',
        )
        first = ensure_default_accounts(self.organization, branch=self.branch)
        second = ensure_default_accounts(self.organization, branch=second_branch)
        self.assertEqual(first['bank'].pk, second['bank'].pk)
        self.assertIsNone(second['bank'].branch_id)

    def test_reversal_creates_counter_entry_and_preserves_original_lines(self):
        entry = self._entry()
        entry.post()
        reversal = entry.reverse(reference='REV-SALE-1', entry_date=date(2026, 5, 2))
        entry.refresh_from_db()
        self.assertEqual(entry.status, 'reversed')
        self.assertEqual(reversal.status, 'posted')
        self.assertEqual(entry.lines.count(), 2)
        self.assertEqual(reversal.total_debit, Decimal('100'))
        self.assertEqual(reversal.total_credit, Decimal('100'))
        self.cash.refresh_from_db()
        self.sales.refresh_from_db()
        self.assertEqual(self.cash.current_balance, Decimal('0'))
        self.assertEqual(self.sales.current_balance, Decimal('0'))

    def test_financial_reports_include_posted_entries_and_respect_organization(self):
        self._entry().post()
        self.client.force_login(self.user)
        response = self.client.get(reverse('trial_balance'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '1000')
        self.assertContains(response, '4000')
        self.assertContains(response, 'As of the end date')
        self.assertEqual(self.client.get(reverse('general_ledger')).status_code, 200)
        self.assertEqual(self.client.get(reverse('profit_and_loss')).status_code, 200)
        balance_sheet = self.client.get(reverse('balance_sheet'))
        self.assertEqual(balance_sheet.status_code, 200)
        self.assertContains(balance_sheet, 'Current earnings included in equity')
        self.assertEqual(self.client.get(reverse('trial_balance') + '?format=csv').status_code, 200)

        other = Organization.objects.create(name='Private Ledger', slug='private-ledger')
        foreign_debit = Account.objects.create(
            organization=other,
            code='9000',
            name='Foreign-only account',
            account_type='asset',
        )
        foreign_credit = Account.objects.create(
            organization=other,
            code='9001',
            name='Foreign-only offset',
            account_type='equity',
        )
        foreign_entry = JournalEntry.objects.create(
            organization=other,
            reference='FOREIGN-LEDGER-ENTRY',
            entry_date=date(2026, 5, 1),
        )
        JournalEntryLine.objects.create(
            entry=foreign_entry,
            account=foreign_debit,
            debit=Decimal('77.00'),
        )
        JournalEntryLine.objects.create(
            entry=foreign_entry,
            account=foreign_credit,
            credit=Decimal('77.00'),
        )
        foreign_entry.post()

        other_branch = Branch.objects.create(
            organization=self.organization,
            name='Other ledger branch',
            code='OTHER-LEDGER',
        )
        branch_debit = Account.objects.create(
            organization=self.organization,
            branch=other_branch,
            code='9000',
            name='Other-branch-only account',
            account_type='asset',
        )
        branch_credit = Account.objects.create(
            organization=self.organization,
            branch=other_branch,
            code='9001',
            name='Other-branch-only offset',
            account_type='equity',
        )
        branch_entry = JournalEntry.objects.create(
            organization=self.organization,
            branch=other_branch,
            reference='OTHER-BRANCH-LEDGER-ENTRY',
            entry_date=date(2026, 5, 1),
        )
        JournalEntryLine.objects.create(
            entry=branch_entry,
            account=branch_debit,
            debit=Decimal('55.00'),
        )
        JournalEntryLine.objects.create(
            entry=branch_entry,
            account=branch_credit,
            credit=Decimal('55.00'),
        )
        branch_entry.post()

        self.assertNotContains(response, 'Private account')
        csv_response = self.client.get(reverse('trial_balance'), {'format': 'csv'})
        self.assertEqual(csv_response.status_code, 200)
        csv_contents = csv_response.content.decode()
        self.assertNotIn('Foreign-only account', csv_contents)
        self.assertNotIn('Other-branch-only account', csv_contents)

    def test_reports_require_authentication_and_reject_invalid_date_ranges(self):
        response = self.client.get(reverse('trial_balance'))
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.user)
        response = self.client.get(reverse('trial_balance') + '?start=2026-06-01&end=2026-05-01')
        self.assertContains(response, 'Start date must not be after end date.')

    def test_navigation_links_implemented_modules_and_reports_to_real_views(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('trial_balance'))

        self.assertContains(response, reverse('inventory_home'))
        self.assertContains(response, reverse('purchase_invoice_list'))
        self.assertContains(response, reverse('sales_invoice_list'))
        self.assertContains(response, reverse('profit_and_loss'))
        self.assertNotContains(response, 'Inventory — incomplete')
        self.assertNotContains(response, 'Purchases — incomplete')
        self.assertNotContains(response, 'Sales — incomplete')
        self.assertNotContains(response, 'href="#"')

    def test_year_end_close_carries_profit_once_and_is_idempotent(self):
        year, period, next_year, retained = self._year_end_fixture()
        self._post_year_activity(year, kind='profit')
        period.close(user=self._organization_finance_user('close-manager'))
        user = self._organization_finance_user('close-user')
        closing = close_financial_year(
            financial_year_id=year.pk,
            user=user,
            reason='Approved annual close',
            retained_earnings_account_id=retained.pk,
        )
        journal_count = JournalEntry.objects.filter(
            reference__startswith=f'YE-',
        ).count()
        repeated = close_financial_year(
            financial_year_id=year.pk,
            user=user,
            reason='Retry',
            retained_earnings_account_id=retained.pk,
        )
        self.assertEqual(repeated.pk, closing.pk)
        self.assertEqual(
            JournalEntry.objects.filter(reference__startswith='YE-').count(),
            journal_count,
        )
        self.assertEqual(closing.status, 'closed')
        self.assertEqual(closing.profit_loss, Decimal('80.00'))
        closing_journal = YearEndClosingJournal.objects.get(closing=closing, role='closing').journal_entry
        opening_journal = YearEndClosingJournal.objects.get(closing=closing, role='opening').journal_entry
        self.assertEqual(closing_journal.status, 'posted')
        self.assertEqual(opening_journal.status, 'posted')
        self.assertEqual(closing_journal.total_debit, closing_journal.total_credit)
        self.assertEqual(opening_journal.total_debit, opening_journal.total_credit)
        self.assertEqual(opening_journal.entry_date, next_year.starts_on)
        self.assertFalse(
            opening_journal.lines.filter(account__account_type__in={'income', 'expense'}).exists()
        )
        retained_opening = opening_journal.lines.get(account=retained)
        self.assertEqual(retained_opening.credit, Decimal('80.00'))
        self.assertEqual(closing.audit_events.count(), 1)
        self.assertEqual(closing.audit_events.get().reason, 'Approved annual close')
        closing_journal.narration = 'Modified after finalization'
        with self.assertRaises(ValidationError):
            closing_journal.save()
        closing_link = YearEndClosingJournal.objects.get(
            closing=closing,
            journal_entry=closing_journal,
        )
        with self.assertRaises(ValidationError):
            closing_link.delete()
        self.cash.refresh_from_db()
        retained.refresh_from_db()
        self.assertEqual(self.cash.current_balance, Decimal('80.00'))
        self.assertEqual(retained.current_balance, Decimal('-80.00'))
        self.client.force_login(user)
        yearly_tb = self.client.get(
            reverse('trial_balance'),
            {'financial_year': year.pk, 'end': year.ends_on.isoformat()},
        )
        self.assertEqual(yearly_tb.status_code, 200)
        self.assertEqual(
            sum((row['debit'] for row in yearly_tb.context['rows'].object_list), Decimal('0')),
            sum((row['credit'] for row in yearly_tb.context['rows'].object_list), Decimal('0')),
        )
        yearly_pnl = self.client.get(
            reverse('profit_and_loss'),
            {'financial_year': year.pk},
        )
        self.assertEqual(yearly_pnl.status_code, 200)
        income_rows = {
            row['code']: row
            for row in yearly_pnl.context['rows'].object_list
        }
        self.assertEqual(income_rows['4000']['balance'], Decimal('0.00'))
        yearly_balance_sheet = self.client.get(
            reverse('balance_sheet'),
            {'financial_year': year.pk},
        )
        sheet_rows = {
            row['code']: row
            for row in yearly_balance_sheet.context['rows'].object_list
        }
        self.assertEqual(sheet_rows['1000']['balance'], Decimal('80.00'))
        self.assertEqual(sheet_rows[retained.code]['balance'], Decimal('80.00'))
        next_year = FinancialYear.objects.get(organization=self.organization, starts_on=date(2026, 2, 1))
        next_year_tb = self.client.get(
            reverse('trial_balance'),
            {'financial_year': next_year.pk},
        )
        self.assertEqual(next_year_tb.status_code, 200)
        self.assertEqual(
            sum((row['debit'] for row in next_year_tb.context['rows'].object_list), Decimal('0')),
            sum((row['credit'] for row in next_year_tb.context['rows'].object_list), Decimal('0')),
        )
        yearly_cash = {
            row['code']: row
            for row in next_year_tb.context['rows'].object_list
        }['1000']
        self.assertEqual(yearly_cash['balance'], Decimal('80.00'))
        cumulative_tb = self.client.get(
            reverse('trial_balance'),
            {'end': next_year.starts_on.isoformat()},
        )
        cumulative_cash = {
            row['code']: row
            for row in cumulative_tb.context['rows'].object_list
        }['1000']
        self.assertEqual(cumulative_cash['balance'], Decimal('80.00'))
        opening_ledger = self.client.get(
            reverse('general_ledger'),
            {'financial_year': next_year.pk, 'account': self.cash.pk},
        )
        self.assertContains(opening_ledger, opening_journal.reference)
        opening_csv = self.client.get(
            reverse('year_end_closing', args=[year.pk]),
            {'format': 'csv'},
        )
        self.assertEqual(opening_csv.status_code, 200)
        self.assertIn(b'1000,Cash,Main,asset,80,0,80', opening_csv.content)
        with self.assertRaises(ValidationError):
            period.reopen(user=user, reason='Attempt after close')

    def test_finalized_close_records_resist_instance_and_bulk_mutations(self):
        year, period, _, retained = self._year_end_fixture()
        self._post_year_activity(year)
        manager = self._organization_finance_user('bulk-protection-manager')
        period.close(user=manager)
        closing = close_financial_year(
            financial_year_id=year.pk,
            user=manager,
            reason='Immutable close',
            retained_earnings_account_id=retained.pk,
        )
        link = YearEndClosingJournal.objects.get(closing=closing, role='closing')
        journal = link.journal_entry
        lines = list(journal.lines.order_by('pk'))
        event = closing.audit_events.get()
        original_reason = closing.reason
        original_narration = journal.narration
        original_references = event.journal_references
        original_line_values = [(line.debit, line.credit) for line in lines]
        period = year.periods.get()
        period_event = period.audit_events.get(action='closed')

        closing.reason = 'Changed'
        with self.assertRaises(ValidationError):
            closing.save()
        with self.assertRaises(ValidationError):
            YearEndClosing.objects.filter(pk=closing.pk).update(reason='Changed')
        with self.assertRaises(ValidationError):
            YearEndClosing.objects.filter(pk=closing.pk).delete()
        with self.assertRaises(ValidationError):
            closing.delete()
        closing.reason = 'Bulk changed'
        with self.assertRaises(ValidationError):
            YearEndClosing.objects.bulk_update([closing], ['reason'])

        year.name = 'Changed year'
        with self.assertRaises(ValidationError):
            FinancialYear.objects.filter(pk=year.pk).update(name='Changed year')
        with self.assertRaises(ValidationError):
            FinancialYear.objects.bulk_update([year], ['name'])
        with self.assertRaises(ValidationError):
            AccountingPeriod.objects.filter(pk=period.pk).update(name='Changed period')
        with self.assertRaises(ValidationError):
            AccountingPeriod.objects.filter(pk=period.pk).delete()
        with self.assertRaises(ValidationError):
            period.delete()
        with self.assertRaises(ValidationError):
            PeriodAuditEvent.objects.filter(pk=period_event.pk).update(reason='Changed')
        with self.assertRaises(ValidationError):
            PeriodAuditEvent.objects.filter(pk=period_event.pk).delete()

        journal.narration = 'Changed'
        with self.assertRaises(ValidationError):
            journal.save()
        with self.assertRaises(ValidationError):
            JournalEntry.objects.filter(pk=journal.pk).update(narration='Changed')
        with self.assertRaises(ValidationError):
            JournalEntry.objects.filter(pk=journal.pk).delete()
        with self.assertRaises(ValidationError):
            journal.delete()
        journal.narration = 'Bulk changed'
        with self.assertRaises(ValidationError):
            JournalEntry.objects.bulk_update([journal], ['narration'])

        with self.assertRaises(ValidationError):
            YearEndClosingJournal.objects.filter(pk=link.pk).update(role='opening')
        with self.assertRaises(ValidationError):
            YearEndClosingJournal.objects.filter(pk=link.pk).delete()
        link.role = 'opening'
        with self.assertRaises(ValidationError):
            YearEndClosingJournal.objects.bulk_update([link], ['role'])
        with self.assertRaises(ValidationError):
            YearEndClosingJournal.objects.bulk_create([
                YearEndClosingJournal(
                    closing=closing,
                    journal_entry=journal,
                    role='opening',
                )
            ])
        with self.assertRaises(ValidationError):
            JournalEntryLine.objects.filter(entry=journal).update(description='Changed')
        with self.assertRaises(ValidationError):
            JournalEntryLine.objects.filter(entry=journal).delete()

        lines[0].description = 'Changed'
        with self.assertRaises(ValidationError):
            JournalEntryLine.objects.bulk_update(lines, ['description'])
        with self.assertRaises(ValidationError):
            JournalEntryLine.objects.bulk_create([
                JournalEntryLine(
                    entry=journal,
                    account=self.cash,
                    debit=Decimal('1.00'),
                )
            ])

        event.reason = 'Changed'
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            YearEndClosingEvent.objects.filter(pk=event.pk).update(reason='Changed')
        with self.assertRaises(ValidationError):
            YearEndClosingEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            YearEndClosingEvent.objects.bulk_create([
                YearEndClosingEvent(
                    closing=closing,
                    action='closed',
                    reason='Fabricated audit record',
                )
            ])
        event.reason = 'Bulk changed'
        with self.assertRaises(ValidationError):
            YearEndClosingEvent.objects.bulk_update([event], ['reason'])
        period_event.reason = 'Changed'
        with self.assertRaises(ValidationError):
            period_event.save()

        with self.assertRaises(ValidationError):
            year.delete()
        with self.assertRaises(ValidationError):
            FinancialYear.objects.filter(pk=year.pk).delete()
        with self.assertRaises(ProtectedError):
            self.organization.delete()

        closing.refresh_from_db()
        link.refresh_from_db()
        journal.refresh_from_db()
        event.refresh_from_db()
        period_event.refresh_from_db()
        self.assertEqual(closing.reason, original_reason)
        self.assertEqual(journal.narration, original_narration)
        self.assertEqual(link.role, 'closing')
        self.assertEqual(event.journal_references, original_references)
        self.assertEqual(
            list(journal.lines.order_by('pk').values_list('debit', 'credit')),
            original_line_values,
        )
        self.assertEqual(YearEndClosing.objects.filter(pk=closing.pk).count(), 1)
        self.assertEqual(YearEndClosingJournal.objects.filter(pk=link.pk).count(), 1)
        self.assertEqual(YearEndClosingEvent.objects.filter(pk=event.pk).count(), 1)
        self.assertEqual(period_event.reason, '')

    def test_unfinalized_records_remain_mutable_through_supported_orm_paths(self):
        year, _, _, _ = self._year_end_fixture()
        draft = JournalEntry.objects.create(
            organization=self.organization,
            branch=self.branch,
            reference='UNFINALIZED-BULK-ENTRY',
            entry_date=year.starts_on,
        )
        self.assertEqual(
            JournalEntry.objects.filter(pk=draft.pk).update(narration='Draft updated'),
            1,
        )
        draft.refresh_from_db()
        self.assertEqual(draft.narration, 'Draft updated')
        line = JournalEntryLine.objects.create(
            entry=draft,
            account=self.cash,
            debit=Decimal('3.00'),
        )
        self.assertEqual(
            JournalEntryLine.objects.filter(pk=line.pk).update(description='Draft line updated'),
            1,
        )
        self.assertEqual(
            JournalEntryLine.objects.filter(pk=line.pk).delete()[0],
            1,
        )

    def test_close_cannot_be_finalized_through_generic_instance_or_bulk_updates(self):
        year, _, _, retained = self._year_end_fixture(suffix='manual-finalize')
        closing = YearEndClosing.objects.create(
            financial_year=year,
            retained_earnings_account=retained,
            reason='Must use close service',
        )
        closing.status = 'closed'
        with self.assertRaises(ValidationError):
            closing.save()
        with self.assertRaises(ValidationError):
            YearEndClosing.objects.filter(pk=closing.pk).update(status='closed')
        with self.assertRaises(ValidationError):
            YearEndClosing.objects.bulk_update([closing], ['status'])
        closing.refresh_from_db()
        self.assertEqual(closing.status, 'closing')

    def test_year_end_close_handles_loss_and_zero_profit_without_duplicate_equity(self):
        for kind, expected_profit in (
            ('loss', Decimal('-100.00')),
            ('zero', Decimal('0.00')),
        ):
            with self.subTest(kind=kind):
                year, period, _, retained = self._year_end_fixture(
                    suffix=kind,
                )
                self._post_year_activity(year, kind=kind, reference=f'YE-ACTIVITY-{kind}')
                manager = self._organization_finance_user(f'period-close-{kind}')
                period.close(user=manager)
                closing = close_financial_year(
                    financial_year_id=year.pk,
                    user=manager,
                    reason='Approved close',
                    retained_earnings_account_id=retained.pk,
                )
                self.assertEqual(closing.profit_loss, expected_profit)
                opening = YearEndClosingJournal.objects.filter(
                    closing=closing,
                    role='opening',
                ).first()
                if expected_profit:
                    self.assertIsNotNone(opening)
                    retained_line = opening.journal_entry.lines.get(account=retained)
                    self.assertEqual(
                        retained_line.debit if expected_profit < 0 else retained_line.credit,
                        abs(expected_profit),
                    )
                else:
                    self.assertIsNotNone(opening)
                    self.assertFalse(opening.journal_entry.lines.filter(account=retained).exists())

    def test_year_end_preview_is_read_only_and_page_requires_organization_access(self):
        year, period, _, retained = self._year_end_fixture()
        self._post_year_activity(year)
        manager = self._organization_finance_user('preview-manager')
        period.close(user=manager)
        entry_count = JournalEntry.objects.count()
        preview = preview_year_end_close(
            financial_year=year,
            retained_earnings_account=retained,
        )
        self.assertTrue(preview['ready'])
        self.assertEqual(JournalEntry.objects.count(), entry_count)
        self.assertFalse(YearEndClosing.objects.filter(financial_year=year).exists())

        self.client.force_login(manager)
        response = self.client.get(reverse('year_end_closing', args=[year.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Proposed next-year opening balances')
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse('year_end_closing', args=[year.pk])).status_code,
            403,
        )

    def test_year_end_readiness_reports_open_period_and_draft_transactions(self):
        year, period, _, retained = self._year_end_fixture()
        manager = self._organization_finance_user('draft-check-manager')
        preview = preview_year_end_close(
            financial_year=year,
            retained_earnings_account=retained,
        )
        self.assertIn('Close every accounting period', ' '.join(preview['errors']))
        period.close(user=manager)
        customer = Customer.objects.create(organization=self.organization, name='Draft customer')
        supplier = Supplier.objects.create(organization=self.organization, name='Draft supplier')
        SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=customer,
            invoice_number='DRAFT-YE-SALE',
            invoice_date=date(2026, 1, 10),
        )
        PurchaseInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=supplier,
            invoice_number='DRAFT-YE-PURCHASE',
            invoice_date=date(2026, 1, 11),
        )
        JournalEntry.objects.create(
            organization=self.organization,
            branch=self.branch,
            reference='DRAFT-YE-JOURNAL',
            entry_date=date(2026, 1, 12),
        )
        CustomerReceipt.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=customer,
            receipt_number='DRAFT-YE-RECEIPT',
            receipt_date=date(2026, 1, 13),
            amount=Decimal('10.00'),
        )
        bank_account = BankAccount.objects.create(
            organization=self.organization,
            branch=self.branch,
            name='Year-end bank',
            account_number='YE-BANK',
        )
        BankTransaction.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=bank_account,
            customer=customer,
            transaction_type='receipt',
            reference_number='DRAFT-YE-BANK',
            transaction_date=date(2026, 1, 14),
            amount=Decimal('10.00'),
        )
        preview = preview_year_end_close(
            financial_year=year,
            retained_earnings_account=retained,
        )
        joined_errors = ' '.join(preview['errors'])
        self.assertIn('draft sales invoices', joined_errors)
        self.assertIn('draft purchase invoices', joined_errors)
        self.assertIn('draft journal entries', joined_errors)
        self.assertIn('draft customer receipts', joined_errors)
        self.assertIn('draft bank transactions', joined_errors)
        self.assertFalse(preview['ready'])

    def test_year_end_close_requires_configured_next_year_and_valid_retained_account(self):
        year, period, _, retained = self._year_end_fixture(include_next_year=False)
        manager = self._organization_finance_user('missing-next-manager')
        period.close(user=manager)
        preview = preview_year_end_close(financial_year=year)
        self.assertTrue(any('immediately following financial year' in error for error in preview['errors']))
        year.retained_earnings_account = None
        year.save(update_fields=['retained_earnings_account'])
        preview = preview_year_end_close(financial_year=year)
        self.assertTrue(any('retained earnings equity account' in error for error in preview['errors']))
        with self.assertRaises(ValidationError):
            close_financial_year(
                financial_year_id=year.pk,
                user=manager,
                reason='Should not close',
                retained_earnings_account_id=retained.pk,
            )
        foreign = Organization.objects.create(name='Foreign close account', slug='foreign-close')
        foreign_equity = Account.objects.create(
            organization=foreign,
            code='3000',
            name='Foreign equity',
            account_type='equity',
        )
        with self.assertRaises(ValidationError):
            close_financial_year(
                financial_year_id=year.pk,
                user=manager,
                reason='Should not close',
                retained_earnings_account_id=foreign_equity.pk,
            )

    def test_year_end_close_rejects_unbalanced_posted_journals_and_inactive_equity(self):
        year, period, _, retained = self._year_end_fixture()
        manager = self._organization_finance_user('invalid-ledger-manager')
        period.close(user=manager)
        invalid = JournalEntry.objects.create(
            organization=self.organization,
            branch=self.branch,
            reference='YE-UNBALANCED-POSTED',
            entry_date=date(2026, 1, 5),
            status='posted',
        )
        JournalEntryLine.objects.bulk_create([
            JournalEntryLine(entry=invalid, account=self.cash, debit=Decimal('12.00')),
        ])
        preview = preview_year_end_close(
            financial_year=year,
            retained_earnings_account=retained,
        )
        self.assertTrue(any('Unbalanced posted journals' in error for error in preview['errors']))
        self.assertTrue(any('trial balance does not balance' in error for error in preview['errors']))
        with self.assertRaises(ValidationError):
            close_financial_year(
                financial_year_id=year.pk,
                user=manager,
                reason='Should not close',
                retained_earnings_account_id=retained.pk,
            )
        retained.is_active = False
        retained.save(update_fields=['is_active'])
        with self.assertRaises(ValidationError):
            close_financial_year(
                financial_year_id=year.pk,
                user=manager,
                reason='Inactive retained earnings',
                retained_earnings_account_id=retained.pk,
            )

    def test_year_end_preview_rejects_historical_cross_organization_journal_lines(self):
        year, _, _, retained = self._year_end_fixture()
        other = Organization.objects.create(name='Historical foreign ledger', slug='historical-foreign')
        foreign_equity = Account.objects.create(
            organization=other,
            code='3001',
            name='Foreign equity',
            account_type='equity',
        )
        historical_entry = JournalEntry.objects.create(
            organization=self.organization,
            branch=self.branch,
            reference='YE-HISTORICAL-FOREIGN-LINE',
            entry_date=year.starts_on.fromordinal(year.starts_on.toordinal() - 1),
            status='posted',
        )
        JournalEntryLine.objects.bulk_create([
            JournalEntryLine(entry=historical_entry, account=self.cash, debit=Decimal('25.00')),
            JournalEntryLine(entry=historical_entry, account=foreign_equity, credit=Decimal('25.00')),
        ])
        manager = self._organization_finance_user('historical-integrity-manager')
        year.periods.get().close(user=manager)

        preview = preview_year_end_close(
            financial_year=year,
            retained_earnings_account=retained,
        )

        self.assertFalse(preview['ready'])
        self.assertTrue(any(
            'organization/branch mismatches' in error
            and historical_entry.reference in error
            for error in preview['errors']
        ))
        with self.assertRaises(ValidationError):
            close_financial_year(
                financial_year_id=year.pk,
                user=manager,
                reason='Reject invalid historical ledger',
                retained_earnings_account_id=retained.pk,
            )
        self.assertFalse(YearEndClosing.objects.filter(financial_year=year).exists())

    def test_branch_close_service_rejects_branch_user_and_branch_journals_preserve_attribution(self):
        year, period, _, retained = self._year_end_fixture(second_branch=True)
        self._post_year_activity(year, reference='YE-BRANCH-ONE-ACTIVITY')
        second_branch = Branch.objects.get(organization=self.organization, code='SECOND')
        second_cash = Account.objects.get(organization=self.organization, code='1002')
        second_revenue = Account.objects.get(organization=self.organization, code='4002')
        entry = JournalEntry.objects.create(
            organization=self.organization,
            branch=second_branch,
            reference='YE-BRANCH-TWO-ACTIVITY',
            entry_date=date(2026, 1, 15),
        )
        JournalEntryLine.objects.create(entry=entry, account=second_cash, debit=Decimal('40.00'))
        JournalEntryLine.objects.create(entry=entry, account=second_revenue, credit=Decimal('40.00'))
        entry.post()
        manager = self._organization_finance_user('multi-branch-manager')
        period.close(user=manager)
        with self.assertRaises(ValidationError):
            close_financial_year(
                financial_year_id=year.pk,
                user=self.user,
                reason='Branch user attempt',
                retained_earnings_account_id=retained.pk,
            )
        closing = close_financial_year(
            financial_year_id=year.pk,
            user=manager,
            reason='Approved multi-branch close',
            retained_earnings_account_id=retained.pk,
        )
        opening_links = YearEndClosingJournal.objects.filter(closing=closing, role='opening')
        self.assertEqual(opening_links.count(), 2)
        self.assertSetEqual(
            set(opening_links.values_list('branch_id', flat=True)),
            {self.branch.pk, second_branch.pk},
        )

    def test_year_end_confirm_route_requires_review_confirmation(self):
        year, period, _, retained = self._year_end_fixture()
        manager = self._organization_finance_user('confirm-manager')
        self._post_year_activity(year)
        period.close(user=manager)
        self.client.force_login(manager)
        response = self.client.post(
            reverse('year_end_closing', args=[year.pk]),
            {
                'action': 'confirm',
                'retained_earnings_account': retained.pk,
                'reason': 'Confirmed close',
                'confirm': '',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(YearEndClosing.objects.filter(financial_year=year).exists())
        self.assertContains(response, 'Confirm that you reviewed the checklist')

    def _year_end_fixture(self, *, suffix='profit', include_next_year=True, second_branch=False):
        year_number = 2027 if suffix == 'zero' else 2026
        starts_on = date(year_number, 1, 1)
        ends_on = date(year_number, 1, 31)
        year = FinancialYear.objects.create(
            organization=self.organization,
            name=f'FY year-end {suffix}',
            starts_on=starts_on,
            ends_on=ends_on,
        )
        period = AccountingPeriod.objects.create(
            financial_year=year,
            name=f'January {suffix}',
            starts_on=starts_on,
            ends_on=ends_on,
        )
        retained = Account.objects.create(
            organization=self.organization,
            code=f'RE-{suffix}'[:25],
            name=f'Retained earnings {suffix}',
            account_type='equity',
        )
        year.retained_earnings_account = retained
        year.save(update_fields=['retained_earnings_account'])
        if include_next_year:
            next_year = FinancialYear.objects.create(
                organization=self.organization,
                name=f'Next FY {suffix}',
                starts_on=date(year_number, 2, 1),
                ends_on=date(year_number, 2, 28),
            )
            AccountingPeriod.objects.create(
                financial_year=next_year,
                name=f'February {suffix}',
                starts_on=next_year.starts_on,
                ends_on=next_year.ends_on,
            )
        if second_branch:
            branch = Branch.objects.create(
                organization=self.organization,
                name='Second',
                code='SECOND',
            )
            Account.objects.create(
                organization=self.organization,
                branch=branch,
                code='1002',
                name='Second branch cash',
                account_type='asset',
            )
            Account.objects.create(
                organization=self.organization,
                branch=branch,
                code='4002',
                name='Second branch revenue',
                account_type='income',
            )
        return year, period, next_year if include_next_year else None, retained

    def _post_year_activity(self, year, *, kind='profit', reference='YE-ACTIVITY'):
        expense, _ = Account.objects.get_or_create(
            organization=self.organization,
            branch=self.branch,
            code='5001',
            defaults={'name': 'Year-end test expense', 'account_type': 'expense'},
        )
        entry = JournalEntry.objects.create(
            organization=self.organization,
            branch=self.branch,
            reference=reference,
            entry_date=year.starts_on + (year.ends_on - year.starts_on) // 2,
        )
        if kind in {'profit', 'zero'}:
            JournalEntryLine.objects.create(entry=entry, account=self.cash, debit=Decimal('100.00'))
            JournalEntryLine.objects.create(entry=entry, account=self.sales, credit=Decimal('100.00'))
        if kind in {'loss', 'zero'}:
            amount = Decimal('100.00')
            JournalEntryLine.objects.create(entry=entry, account=expense, debit=amount)
            JournalEntryLine.objects.create(entry=entry, account=self.cash, credit=amount)
        if kind == 'profit':
            expense_entry = JournalEntry.objects.create(
                organization=self.organization,
                branch=self.branch,
                reference=f'{reference}-EXPENSE',
                entry_date=entry.entry_date,
            )
            JournalEntryLine.objects.create(
                entry=expense_entry,
                account=expense,
                debit=Decimal('20.00'),
            )
            JournalEntryLine.objects.create(
                entry=expense_entry,
                account=self.cash,
                credit=Decimal('20.00'),
            )
            expense_entry.post()
        entry.post()
