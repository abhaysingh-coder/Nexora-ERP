from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounting.models import Account, JournalEntry, JournalEntryLine
from banking.models import BankAccount
from core.models import User
from inventory.models import Product, Warehouse
from organizations.models import Branch, Organization, OrganizationMembership
from purchases.models import PurchaseInvoice, PurchaseInvoiceLine, Supplier
from sales.models import Customer, SalesInvoice, SalesInvoiceLine


class DashboardIsolationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name='Dashboard tenant A',
            slug='dashboard-tenant-a',
        )
        self.other_organization = Organization.objects.create(
            name='Dashboard tenant B',
            slug='dashboard-tenant-b',
        )
        self.branch_a = Branch.objects.create(
            organization=self.organization,
            name='Dashboard branch A',
            code='DASH-A',
        )
        self.branch_b = Branch.objects.create(
            organization=self.organization,
            name='Dashboard branch B',
            code='DASH-B',
        )
        self.foreign_branch = Branch.objects.create(
            organization=self.other_organization,
            name='Foreign dashboard branch',
            code='DASH-F',
        )
        self.warehouse_a = self._warehouse(self.organization, self.branch_a, 'A')
        self.warehouse_b = self._warehouse(self.organization, self.branch_b, 'B')
        self.foreign_warehouse = self._warehouse(
            self.other_organization,
            self.foreign_branch,
            'F',
        )

        self.branch_user = self._user(
            'dashboard-branch-user',
            branch=self.branch_a,
        )
        self.organization_user = self._user('dashboard-organization-user')

        self.main_product = self._product(
            self.organization,
            'DASH-MAIN',
            'Shared organization catalog item',
            reorder_level=Decimal('2'),
        )
        self.branch_b_product = self._product(
            self.organization,
            'DASH-BRANCH-B',
            'Branch B stock item',
            reorder_level=Decimal('2'),
        )
        self.low_product = self._product(
            self.organization,
            'DASH-LOW',
            'No stock item',
            reorder_level=Decimal('1'),
        )
        self.unallocated_product = Product.objects.create(
            organization=self.organization,
            sku='DASH-UNALLOCATED',
            name='Legacy stock without warehouse allocation',
            cost_price=Decimal('1'),
            sales_price=Decimal('10'),
            reorder_level=Decimal('1'),
            stock_quantity=Decimal('1'),
        )
        self.foreign_product = self._product(
            self.other_organization,
            'DASH-FOREIGN',
            'Foreign stock item',
            reorder_level=Decimal('1'),
        )

        self.customer_a = Customer.objects.create(
            organization=self.organization,
            name='Dashboard customer A',
        )
        self.customer_b = Customer.objects.create(
            organization=self.organization,
            name='Dashboard customer B',
        )
        self.foreign_customer = Customer.objects.create(
            organization=self.other_organization,
            name='Foreign dashboard customer',
        )
        self.supplier_a = Supplier.objects.create(
            organization=self.organization,
            name='Dashboard supplier A',
        )
        self.supplier_b = Supplier.objects.create(
            organization=self.organization,
            name='Dashboard supplier B',
        )
        self.foreign_supplier = Supplier.objects.create(
            organization=self.other_organization,
            name='Foreign dashboard supplier',
        )

        self._post_purchase(
            self.organization,
            self.branch_a,
            self.supplier_a,
            self.main_product,
            'DASH-PI-A',
            Decimal('10'),
            Decimal('2'),
        )
        self._post_purchase(
            self.organization,
            self.branch_b,
            self.supplier_b,
            self.main_product,
            'DASH-PI-B',
            Decimal('10'),
            Decimal('3'),
        )
        self._post_purchase(
            self.organization,
            self.branch_b,
            self.supplier_b,
            self.branch_b_product,
            'DASH-PI-B-LOW',
            Decimal('7'),
            Decimal('3'),
        )
        self._post_purchase(
            self.other_organization,
            self.foreign_branch,
            self.foreign_supplier,
            self.foreign_product,
            'DASH-PI-F',
            Decimal('10'),
            Decimal('9'),
        )

        self._post_sale(
            self.organization,
            self.branch_a,
            self.customer_a,
            self.main_product,
            'DASH-SI-A',
            Decimal('1'),
            Decimal('2'),
            Decimal('10'),
        )
        self._post_sale(
            self.organization,
            self.branch_b,
            self.customer_b,
            self.main_product,
            'DASH-SI-B',
            Decimal('1'),
            Decimal('3'),
            Decimal('20'),
        )
        self._post_sale(
            self.other_organization,
            self.foreign_branch,
            self.foreign_customer,
            self.foreign_product,
            'DASH-SI-F',
            Decimal('1'),
            Decimal('9'),
            Decimal('90'),
        )

        self._create_bank_account(
            self.organization,
            self.branch_a,
            'Branch A cash',
            'DASH-CASH-A',
            Decimal('10'),
            'cash',
        )
        self._create_bank_account(
            self.organization,
            self.branch_a,
            'Branch A bank',
            'DASH-BANK-A',
            Decimal('20'),
            'bank',
        )
        self._create_bank_account(
            self.organization,
            self.branch_b,
            'Branch B cash',
            'DASH-CASH-B',
            Decimal('40'),
            'cash',
        )
        self._create_bank_account(
            self.organization,
            self.branch_b,
            'Branch B bank',
            'DASH-BANK-B',
            Decimal('30'),
            'bank',
        )
        self._create_bank_account(
            self.organization,
            None,
            'Shared cash',
            'DASH-CASH-SHARED',
            Decimal('5'),
            'cash',
        )
        self._create_bank_account(
            self.other_organization,
            self.foreign_branch,
            'Foreign bank',
            'DASH-BANK-F',
            Decimal('900'),
            'bank',
        )

        self._post_branch_null_working_capital_entries()
        self._post_recent_journal(
            self.organization,
            self.branch_a,
            'DASH-RECENT-A',
        )
        self._post_recent_journal(
            self.organization,
            self.branch_b,
            'DASH-RECENT-B',
        )
        self._post_recent_journal(
            self.organization,
            None,
            'DASH-RECENT-SHARED',
        )
        self._post_recent_journal(
            self.other_organization,
            self.foreign_branch,
            'DASH-RECENT-F',
        )

    def _user(self, username, branch=None):
        user = User.objects.create_user(
            username=username,
            email=f'{username}@example.test',
            password='test-password',
            organization=self.organization,
            branch=branch,
        )
        OrganizationMembership.objects.create(
            user=user,
            organization=self.organization,
            role='owner',
        )
        return user

    def _warehouse(self, organization, branch, suffix):
        return Warehouse.objects.create(
            organization=organization,
            branch=branch,
            name=f'Dashboard warehouse {suffix}',
            code=f'DASH-WH-{suffix}',
        )

    def _product(self, organization, sku, name, *, reorder_level):
        return Product.objects.create(
            organization=organization,
            sku=sku,
            name=name,
            cost_price=Decimal('1'),
            sales_price=Decimal('10'),
            reorder_level=reorder_level,
        )

    def _post_purchase(
        self,
        organization,
        branch,
        supplier,
        product,
        number,
        quantity,
        unit_cost,
        *,
        invoice_date=None,
    ):
        invoice = PurchaseInvoice.objects.create(
            organization=organization,
            branch=branch,
            supplier=supplier,
            invoice_number=number,
            invoice_date=invoice_date or date(2026, 10, 10),
            due_date=date(2026, 11, 10),
            subtotal=quantity * unit_cost,
            total_amount=quantity * unit_cost,
        )
        PurchaseInvoiceLine.objects.create(
            invoice=invoice,
            product=product,
            quantity=quantity,
            unit_cost=unit_cost,
            line_total=quantity * unit_cost,
        )
        invoice.post()
        return invoice

    def _post_sale(
        self,
        organization,
        branch,
        customer,
        product,
        number,
        quantity,
        unit_cost,
        unit_price,
        *,
        invoice_date=None,
    ):
        invoice = SalesInvoice.objects.create(
            organization=organization,
            branch=branch,
            customer=customer,
            invoice_number=number,
            invoice_date=invoice_date or date(2026, 10, 20),
            due_date=date(2026, 11, 20),
            subtotal=quantity * unit_price,
            total_amount=quantity * unit_price,
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=product,
            quantity=quantity,
            unit_cost=unit_cost,
            unit_price=unit_price,
            line_total=quantity * unit_price,
        )
        invoice.post()
        return invoice

    def _create_bank_account(self, organization, branch, name, number, balance, account_type):
        return BankAccount.objects.create(
            organization=organization,
            branch=branch,
            name=name,
            account_type=account_type,
            account_number=number,
            opening_balance=balance,
            current_balance=balance,
        )

    def _post_branch_null_working_capital_entries(self):
        accounts = {
            account.code: account
            for account in Account.objects.filter(organization=self.organization)
        }
        cases = (
            ('DASH-NULL-AR', '1100', '4000', Decimal('5')),
            ('DASH-NULL-AP', '1001', '2000', Decimal('7')),
        )
        for reference, debit_code, credit_code, amount in cases:
            entry = JournalEntry.objects.create(
                organization=self.organization,
                branch=None,
                reference=reference,
                entry_date=date(2026, 10, 25),
            )
            JournalEntryLine.objects.create(
                entry=entry,
                account=accounts[debit_code],
                debit=amount,
            )
            JournalEntryLine.objects.create(
                entry=entry,
                account=accounts[credit_code],
                credit=amount,
            )
            entry.post()

    def _post_recent_journal(self, organization, branch, reference):
        cash_account = Account.objects.get(
            organization=organization,
            code='1001',
        )
        equity_account = Account.objects.get(
            organization=organization,
            code='3000',
        )
        entry = JournalEntry.objects.create(
            organization=organization,
            branch=branch,
            reference=reference,
            entry_date=timezone.localdate(),
        )
        JournalEntryLine.objects.create(
            entry=entry,
            account=cash_account,
            debit=Decimal('1'),
        )
        JournalEntryLine.objects.create(
            entry=entry,
            account=equity_account,
            credit=Decimal('1'),
        )
        entry.post()

    def test_branch_dashboard_metrics_exclude_other_branch_and_foreign_tenant(self):
        self.client.force_login(self.branch_user)
        session = self.client.session
        session['active_organization_id'] = self.other_organization.pk
        session.save()

        response = self.client.get(
            reverse('dashboard'),
            {
                'organization': self.other_organization.pk,
                'branch': self.branch_b.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['organization'], self.organization)
        self.assertEqual(response.context['sales_total'], Decimal('10'))
        self.assertEqual(response.context['purchase_total'], Decimal('20'))
        self.assertEqual(response.context['cash_balance'], Decimal('30'))
        self.assertEqual(response.context['receivables'], Decimal('10'))
        self.assertEqual(response.context['payables'], Decimal('20'))
        self.assertEqual(response.context['shared_receivables'], Decimal('5'))
        self.assertEqual(response.context['shared_payables'], Decimal('7'))
        self.assertEqual(response.context['inventory_value'], Decimal('18.00'))
        self.assertFalse(response.context['inventory_value_complete'])
        self.assertEqual(response.context['inventory_valuation_exceptions'], 1)
        self.assertEqual(response.context['low_stock_count'], 3)

        for value in ('10', '20', '30', '3', '18.00', '5', '7', '1'):
            self.assertContains(response, f'<strong>{value}</strong>')
        self.assertContains(response, 'Valuation exceptions')
        self.assertContains(response, 'DASH-RECENT-A')
        self.assertNotContains(response, 'DASH-RECENT-B')
        self.assertContains(response, 'DASH-RECENT-SHARED')
        self.assertContains(response, 'SHARED / UNALLOCATED')
        self.assertNotContains(response, 'DASH-RECENT-F')
        self.assertNotIn('tax_summary', response.context)
        self.assertNotIn('tax_total', response.context)

        shared_entry = JournalEntry.objects.get(reference='DASH-NULL-AR')
        detail_response = self.client.get(
            reverse('journal_entry_detail', args=(shared_entry.pk,))
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, 'Shared / unallocated')
        self.assertContains(detail_response, 'DASH-NULL-AR')

    def test_organization_dashboard_metrics_include_its_branches_and_shared_records_only(self):
        self.client.force_login(self.organization_user)
        session = self.client.session
        session['active_organization_id'] = self.other_organization.pk
        session.save()

        response = self.client.get(
            reverse('dashboard'),
            {
                'organization': self.other_organization.pk,
                'branch': self.foreign_branch.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['organization'], self.organization)
        self.assertEqual(response.context['sales_total'], Decimal('30'))
        self.assertEqual(response.context['purchase_total'], Decimal('71'))
        self.assertEqual(response.context['cash_balance'], Decimal('105'))
        self.assertEqual(response.context['receivables'], Decimal('35'))
        self.assertEqual(response.context['payables'], Decimal('78'))
        self.assertEqual(response.context['shared_receivables'], Decimal('0'))
        self.assertEqual(response.context['shared_payables'], Decimal('0'))
        self.assertEqual(response.context['inventory_value'], Decimal('66.00'))
        self.assertFalse(response.context['inventory_value_complete'])
        self.assertEqual(response.context['inventory_valuation_exceptions'], 1)
        self.assertEqual(response.context['low_stock_count'], 1)

        for value in ('30', '71', '105', '1', '66.00', '35', '78'):
            self.assertContains(response, f'<strong>{value}</strong>')
        self.assertContains(response, 'Valuation exceptions')
        self.assertContains(response, 'DASH-RECENT-A')
        self.assertContains(response, 'DASH-RECENT-B')
        self.assertContains(response, 'DASH-RECENT-SHARED')
        self.assertNotContains(response, 'DASH-RECENT-F')
        self.assertNotContains(response, 'DASH-SI-F')
        self.assertNotContains(response, 'DASH-PI-F')

    def test_branch_null_invoices_are_excluded_from_branch_totals_but_included_for_organization(self):
        today = timezone.localdate()
        self._post_purchase(
            self.organization,
            None,
            self.supplier_a,
            self.main_product,
            'DASH-NULL-PI',
            Decimal('2'),
            Decimal('4'),
            invoice_date=today,
        )
        self._post_sale(
            self.organization,
            None,
            self.customer_a,
            self.main_product,
            'DASH-NULL-SI',
            Decimal('1'),
            Decimal('4'),
            Decimal('7'),
            invoice_date=today,
        )

        self.client.force_login(self.branch_user)
        branch_response = self.client.get(
            reverse('dashboard'),
            {'organization': self.organization.pk, 'branch': ''},
        )

        self.assertEqual(branch_response.status_code, 200)
        self.assertEqual(branch_response.context['sales_total'], Decimal('10'))
        self.assertEqual(branch_response.context['purchase_total'], Decimal('20'))
        self.assertEqual(branch_response.context['receivables'], Decimal('10'))
        self.assertEqual(branch_response.context['payables'], Decimal('20'))
        self.assertEqual(branch_response.context['shared_receivables'], Decimal('12'))
        self.assertEqual(branch_response.context['shared_payables'], Decimal('15'))
        self.assertEqual(branch_response.context['inventory_value'], Decimal('23.64'))
        self.assertContains(branch_response, '<strong>10</strong>')
        self.assertContains(branch_response, '<strong>20</strong>')
        self.assertContains(branch_response, 'PI-DASH-NULL-PI')
        self.assertContains(branch_response, 'SI-DASH-NULL-SI')
        self.assertContains(branch_response, 'Shared / unallocated receivables')
        self.assertContains(branch_response, 'Shared / unallocated payables')

        self.client.force_login(self.organization_user)
        organization_response = self.client.get(
            reverse('dashboard'),
            {'organization': self.organization.pk, 'branch': self.branch_b.pk},
        )

        self.assertEqual(organization_response.status_code, 200)
        self.assertEqual(organization_response.context['sales_total'], Decimal('37'))
        self.assertEqual(organization_response.context['purchase_total'], Decimal('79'))
        self.assertEqual(organization_response.context['receivables'], Decimal('42'))
        self.assertEqual(organization_response.context['payables'], Decimal('86'))
        self.assertEqual(organization_response.context['inventory_value'], Decimal('71.64'))
        self.assertContains(organization_response, '<strong>37</strong>')
        self.assertContains(organization_response, '<strong>79</strong>')
        self.assertContains(organization_response, 'PI-DASH-NULL-PI')
        self.assertContains(organization_response, 'SI-DASH-NULL-SI')
