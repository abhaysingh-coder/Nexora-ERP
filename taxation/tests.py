from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounting.models import JournalEntry
from core.models import User
from inventory.models import Product, StockMovement, Warehouse, adjust_stock
from organizations.models import Branch, Organization, OrganizationMembership
from purchases.models import PurchaseInvoice, PurchaseInvoiceLine, Supplier
from sales.models import Customer, SalesInvoice, SalesInvoiceLine
from taxation.forms import TaxRateForm
from taxation.models import (
    ProductTaxClassification,
    TaxCode,
    TaxRate,
    TaxRegistration,
    TaxTransaction,
    validate_gstin,
)
from taxation.services import calculate_tax


class TaxationTestCase(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Tax Co', slug='tax-co')
        self.other_organization = Organization.objects.create(name='Other Co', slug='other-co')
        self.branch = Branch.objects.create(organization=self.organization, name='Mumbai', code='MUM')
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            branch=self.branch,
            name='Main',
            code='MAIN',
        )
        self.product = Product.objects.create(
            organization=self.organization,
            sku='TAX-1',
            name='Taxable item',
            cost_price=Decimal('30'),
            sales_price=Decimal('100'),
        )
        adjust_stock(
            self.product,
            self.warehouse,
            Decimal('10'),
            'adjustment',
            'TaxTestOpening',
            1,
            Decimal('30'),
        )
        self.tax_code = TaxCode.objects.create(
            organization=self.organization,
            code='GST18',
            name='GST 18 percent',
        )
        self._rate('cgst', '9')
        self._rate('sgst', '9')
        self._rate('igst', '18')
        TaxRegistration.objects.create(
            organization=self.organization,
            branch=self.branch,
            gstin='27AAAAA0000A1Z5',
            state_code='27',
            is_registered=True,
        )
        self.customer = Customer.objects.create(
            organization=self.organization,
            name='Customer',
            gstin='27BBBBB0000A1Z5',
            state_code='27',
        )
        self.supplier = Supplier.objects.create(
            organization=self.organization,
            name='Supplier',
            gstin='27CCCCC0000A1Z5',
            state_code='27',
        )
        self.user = User.objects.create_user(
            username='tax-user',
            email='tax-user@example.test',
            password='test-password',
            organization=self.organization,
            branch=self.branch,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='accountant',
        )

    def _rate(self, tax_type, rate, effective_from=date(2026, 1, 1), effective_to=None, state_code=''):
        return TaxRate.objects.create(
            organization=self.organization,
            tax_code=self.tax_code,
            tax_type=tax_type,
            rate=Decimal(rate),
            state_code=state_code,
            effective_from=effective_from,
            effective_to=effective_to,
        )

    def _calculate(self, **overrides):
        arguments = {
            'organization': self.organization,
            'tax_code': self.tax_code,
            'transaction_date': date(2026, 2, 1),
            'taxable_amount': Decimal('100.00'),
            'origin_state': '27',
            'place_of_supply': '27',
        }
        arguments.update(overrides)
        return calculate_tax(**arguments)

    def test_intra_state_calculation_splits_cgst_and_sgst(self):
        result = self._calculate()
        self.assertEqual(result['cgst'], Decimal('9.00'))
        self.assertEqual(result['sgst'], Decimal('9.00'))
        self.assertEqual(result['igst'], Decimal('0.00'))
        self.assertEqual(result['invoice_total'], Decimal('118.00'))

    def test_inter_state_calculation_uses_igst(self):
        result = self._calculate(place_of_supply='29')
        self.assertEqual(result['cgst'], Decimal('0.00'))
        self.assertEqual(result['sgst'], Decimal('0.00'))
        self.assertEqual(result['igst'], Decimal('18.00'))

    def test_tax_inclusive_and_rounding(self):
        result = self._calculate(taxable_amount=Decimal('118.00'), is_inclusive=True)
        self.assertEqual(result['taxable_value'], Decimal('100.00'))
        self.assertEqual(result['total_tax'], Decimal('18.00'))
        self.assertEqual(result['invoice_total'], Decimal('118.00'))

        rounded = self._calculate(taxable_amount=Decimal('0.05'))
        self.assertEqual(rounded['cgst'] + rounded['sgst'], rounded['total_tax'])
        self.assertEqual(rounded['invoice_total'], rounded['taxable_value'] + rounded['total_tax'])

    def test_utgst_is_used_for_configured_union_territory_component_rates(self):
        self._rate('cgst', '9', state_code='04')
        self._rate('utgst', '9', state_code='04')
        result = self._calculate(origin_state='04', place_of_supply='04')
        self.assertEqual(result['cgst'], Decimal('9.00'))
        self.assertEqual(result['utgst'], Decimal('9.00'))
        self.assertEqual(result['sgst'], Decimal('0.00'))

    def test_discounted_sales_line_uses_discounted_taxable_amount(self):
        ProductTaxClassification.objects.create(
            organization=self.organization,
            product=self.product,
            tax_code=self.tax_code,
        )
        invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            invoice_number='GST-SALE-DISCOUNT',
            invoice_date=date(2026, 2, 1),
            place_of_supply='27',
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=Decimal('1'),
            unit_cost=Decimal('30'),
            unit_price=Decimal('100'),
            line_total=Decimal('100'),
            discount_amount=Decimal('10'),
        )
        invoice.post()
        invoice.refresh_from_db()
        self.assertEqual(invoice.subtotal, Decimal('90.00'))
        self.assertEqual(invoice.tax_amount, Decimal('16.20'))
        self.assertEqual(invoice.total_amount, Decimal('106.20'))

    def test_repeated_product_lines_are_taxed_separately_but_stock_moves_once(self):
        ProductTaxClassification.objects.create(
            organization=self.organization,
            product=self.product,
            tax_code=self.tax_code,
        )
        invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            invoice_number='GST-SALE-MULTILINE',
            invoice_date=date(2026, 2, 1),
            place_of_supply='27',
        )
        for amount in ('40', '60'):
            SalesInvoiceLine.objects.create(
                invoice=invoice,
                product=self.product,
                quantity=Decimal('1'),
                unit_cost=Decimal('30'),
                unit_price=Decimal(amount),
                line_total=Decimal(amount),
            )
        invoice.post()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('8.000'))
        self.assertEqual(
            StockMovement.objects.filter(
                reference_model='SalesInvoice',
                reference_id=invoice.pk,
            ).count(),
            1,
        )
        self.assertEqual(
            TaxTransaction.objects.filter(source_model='SalesInvoice', source_id=invoice.pk).count(),
            2,
        )

    def test_effective_rate_selection_uses_transaction_date(self):
        self._rate(
            'cgst',
            '8',
            effective_from=date(2025, 1, 1),
            effective_to=date(2025, 12, 31),
        )
        self._rate(
            'sgst',
            '8',
            effective_from=date(2025, 1, 1),
            effective_to=date(2025, 12, 31),
        )
        old = self._calculate(transaction_date=date(2025, 6, 1))
        current = self._calculate(transaction_date=date(2026, 2, 1))
        self.assertEqual(old['cgst'], Decimal('8.00'))
        self.assertEqual(current['cgst'], Decimal('9.00'))

    def test_exempt_and_zero_rated_lines_have_no_tax(self):
        exempt = self._calculate(is_exempt=True)
        zero_rated = self._calculate(is_zero_rated=True)
        self.assertEqual(exempt['total_tax'], Decimal('0.00'))
        self.assertEqual(zero_rated['total_tax'], Decimal('0.00'))
        self.assertTrue(zero_rated['is_zero_rated'])

    def test_missing_or_invalid_place_of_supply_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._calculate(place_of_supply='')
        with self.assertRaises(ValidationError):
            self._calculate(place_of_supply='XX')
        with self.assertRaises(ValidationError):
            self._calculate(origin_state='')

    def test_gstin_and_tax_rate_validation(self):
        validate_gstin('27AAAAA0000A1Z5')
        with self.assertRaises(ValidationError):
            validate_gstin('not-a-gstin')
        foreign_code = TaxCode.objects.create(organization=self.other_organization, code='FOREIGN', name='Foreign')
        form = TaxRateForm(
            {
                'tax_code': foreign_code.pk,
                'tax_type': 'cgst',
                'rate': '101',
                'effective_from': '2026-01-01',
                'is_active': 'on',
            }
        )
        form.fields['tax_code'].queryset = TaxCode.objects.filter(organization=self.organization)
        self.assertFalse(form.is_valid())

        valid_code_rate_form = TaxRateForm(
            {
                'tax_code': self.tax_code.pk,
                'tax_type': 'cess',
                'rate': '101',
                'effective_from': '2026-01-01',
                'is_active': 'on',
            }
        )
        valid_code_rate_form.instance.organization = self.organization
        valid_code_rate_form.fields['tax_code'].queryset = TaxCode.objects.filter(
            organization=self.organization
        )
        self.assertFalse(valid_code_rate_form.is_valid())
        with self.assertRaises(ValidationError):
            TaxRate.objects.create(
                organization=self.organization,
                tax_code=foreign_code,
                tax_type='cgst',
                rate=Decimal('5'),
                effective_from=date(2026, 1, 1),
            )
        with self.assertRaises(ValidationError):
            Customer.objects.create(
                organization=self.organization,
                name='Invalid GSTIN',
                gstin='bad-value',
            )

    def test_sales_posting_persists_tax_balances_and_is_idempotent(self):
        ProductTaxClassification.objects.create(
            organization=self.organization,
            product=self.product,
            tax_code=self.tax_code,
            hsn_sac='1234',
        )
        invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            invoice_number='GST-SALE-1',
            invoice_date=date(2026, 2, 1),
            place_of_supply='27',
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=Decimal('1'),
            unit_cost=Decimal('30'),
            unit_price=Decimal('100'),
            line_total=Decimal('100'),
        )
        invoice.post()
        invoice.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(invoice.subtotal, Decimal('100.00'))
        self.assertEqual(invoice.tax_amount, Decimal('18.00'))
        self.assertEqual(invoice.total_amount, Decimal('118.00'))
        self.assertEqual(self.product.stock_quantity, Decimal('9.000'))
        self.assertEqual(TaxTransaction.objects.filter(source_model='SalesInvoice').count(), 1)
        tax_row = TaxTransaction.objects.get(source_model='SalesInvoice')
        self.assertEqual(tax_row.cgst_rate, Decimal('9.0000'))
        self.assertEqual(tax_row.sgst_rate, Decimal('9.0000'))
        self.assertEqual(tax_row.origin_state, '27')
        self.assertEqual(tax_row.place_of_supply, '27')
        invoice.post()
        self.assertEqual(TaxTransaction.objects.filter(source_model='SalesInvoice').count(), 1)
        self.assertEqual(JournalEntry.objects.filter(reference__startswith='SI-GST-SALE-1').count(), 2)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('9.000'))
        self.assertTrue(
            all(
                entry.total_debit == entry.total_credit
                for entry in JournalEntry.objects.filter(reference__startswith='SI-GST-SALE-1')
            )
        )

    def test_purchase_posting_posts_eligible_input_tax_and_journal_balances(self):
        ProductTaxClassification.objects.create(
            organization=self.organization,
            product=self.product,
            tax_code=self.tax_code,
            hsn_sac='1234',
        )
        invoice = PurchaseInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=self.supplier,
            invoice_number='GST-PURCHASE-1',
            invoice_date=date(2026, 2, 1),
            place_of_supply='27',
        )
        PurchaseInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=Decimal('2'),
            unit_cost=Decimal('50'),
            line_total=Decimal('100'),
        )
        invoice.post()
        invoice.refresh_from_db()
        self.assertEqual(invoice.tax_amount, Decimal('18.00'))
        self.assertEqual(invoice.total_amount, Decimal('118.00'))
        self.assertTrue(JournalEntry.objects.filter(reference='PI-GST-PURCHASE-1', status='posted').exists())
        self.assertEqual(TaxTransaction.objects.filter(source_model='PurchaseInvoice').count(), 1)
        entry = JournalEntry.objects.get(reference='PI-GST-PURCHASE-1')
        self.assertEqual(entry.total_debit, entry.total_credit)

    def test_purchase_reverse_charge_records_tax_but_excludes_it_from_supplier_payable(self):
        ProductTaxClassification.objects.create(
            organization=self.organization,
            product=self.product,
            tax_code=self.tax_code,
            is_reverse_charge=True,
        )
        invoice = PurchaseInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=self.supplier,
            invoice_number='GST-PURCHASE-RCM',
            invoice_date=date(2026, 2, 1),
            place_of_supply='27',
        )
        PurchaseInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=Decimal('1'),
            unit_cost=Decimal('50'),
            line_total=Decimal('100'),
        )
        invoice.post()
        invoice.refresh_from_db()
        entry = JournalEntry.objects.get(reference='PI-GST-PURCHASE-RCM')
        self.assertEqual(invoice.total_amount, Decimal('100.00'))
        self.assertEqual(invoice.tax_amount, Decimal('0.00'))
        self.assertEqual(entry.total_debit, entry.total_credit)
        self.assertTrue(
            TaxTransaction.objects.get(source_model='PurchaseInvoice').is_reverse_charge
        )

    def test_tax_posting_failure_rolls_back_stock_journal_and_tax_rows(self):
        ProductTaxClassification.objects.create(
            organization=self.organization,
            product=self.product,
            tax_code=self.tax_code,
        )
        invoice = PurchaseInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=self.supplier,
            invoice_number='GST-ROLLBACK-1',
            invoice_date=date(2026, 2, 1),
            place_of_supply='27',
        )
        PurchaseInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=Decimal('1'),
            unit_cost=Decimal('50'),
            line_total=Decimal('100'),
        )
        from taxation.services import persist_tax_transaction as persist_original

        def persist_then_fail(*args, **kwargs):
            persist_original(*args, **kwargs)
            raise ValidationError('Simulated tax snapshot failure')

        with patch(
            'taxation.services.persist_tax_transaction',
            side_effect=persist_then_fail,
        ):
            with self.assertRaises(ValidationError):
                invoice.post()
        self.product.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('10.000'))
        self.assertEqual(invoice.status, 'draft')
        self.assertFalse(TaxTransaction.objects.filter(source_model='PurchaseInvoice').exists())
        self.assertFalse(JournalEntry.objects.filter(reference='PI-GST-ROLLBACK-1').exists())

    def test_taxation_pages_are_authenticated_and_scoped_to_user_organization(self):
        self.client.force_login(self.user)
        urls = [
            'taxation_home',
            'tax_code_create',
            'tax_rate_create',
            'tax_registration_create',
            'product_tax_classification_create',
            'sales_tax_register',
            'purchase_tax_register',
            'output_tax_summary',
            'input_tax_summary',
            'tax_liability_summary',
        ]
        for url_name in urls:
            with self.subTest(url=url_name):
                self.assertEqual(self.client.get(reverse(url_name)).status_code, 200)

        private_code = TaxCode.objects.create(
            organization=self.other_organization,
            code='PRIVATE',
            name='Private tax code',
        )
        response = self.client.get(reverse('taxation_home'))
        self.assertContains(response, 'GST18')
        self.assertNotContains(response, private_code.code)

    def test_branch_user_cannot_see_another_branch_registration(self):
        another_branch = Branch.objects.create(
            organization=self.organization,
            name='Delhi',
            code='DEL',
        )
        TaxRegistration.objects.create(
            organization=self.organization,
            branch=another_branch,
            gstin='29AAAAA0000A1Z5',
            state_code='29',
            is_registered=True,
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('taxation_home'))
        self.assertContains(response, '27AAAAA0000A1Z5')
        self.assertNotContains(response, '29AAAAA0000A1Z5')

    def test_tax_configuration_requires_finance_role_and_writes_audit_event(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('tax_code_create'),
            {
                'code': 'GST5',
                'name': 'Reduced tax',
                'category': 'goods',
                'is_active': 'on',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TaxCode.objects.filter(organization=self.organization, code='GST5').exists())
        self.assertTrue(
            self.user.auditevent_set.filter(action='taxation.configuration_saved').exists()
        )

        ordinary_user = User.objects.create_user(
            username='ordinary',
            email='ordinary@example.test',
            password='test-password',
            organization=self.organization,
            is_staff=True,
        )
        OrganizationMembership.objects.create(
            user=ordinary_user,
            organization=self.organization,
            role='staff',
        )
        self.client.force_login(ordinary_user)
        self.assertEqual(self.client.get(reverse('tax_rate_create')).status_code, 403)

    def test_csv_export_includes_headers_and_respects_organization_scope(self):
        tax_row = TaxTransaction.objects.create(
            organization=self.organization,
            branch=self.branch,
            transaction_kind='sale',
            transaction_date=date(2026, 2, 1),
            source_model='SalesInvoice',
            source_id=10,
            tax_code=self.tax_code,
            taxable_value=Decimal('100'),
            cgst=Decimal('9'),
            sgst=Decimal('9'),
            total_tax=Decimal('18'),
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('tax_register_csv'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'CGST,SGST,IGST,UTGST,Cess', response.content)
        self.assertIn(b'SalesInvoice-10', response.content)

        other_code = TaxCode.objects.create(
            organization=self.other_organization,
            code='OTHER',
            name='Other',
        )
        TaxTransaction.objects.create(
            organization=self.other_organization,
            transaction_kind='sale',
            transaction_date=date(2026, 2, 1),
            source_model='SalesInvoice',
            source_id=11,
            tax_code=other_code,
        )
        response = self.client.get(reverse('tax_register_csv'))
        self.assertNotIn(b'SalesInvoice-11', response.content)

    def test_tax_csv_keeps_branch_and_tenant_scope_under_filter_tampering(self):
        other_branch = Branch.objects.create(
            organization=self.organization,
            name='Other tax branch',
            code='OTHER-TAX',
        )
        other_branch_row = TaxTransaction.objects.create(
            organization=self.organization,
            branch=other_branch,
            transaction_kind='sale',
            transaction_date=date(2026, 3, 1),
            source_model='SalesInvoice',
            source_id=202,
            tax_code=self.tax_code,
        )
        foreign_code = TaxCode.objects.create(
            organization=self.other_organization,
            code='FOREIGN-TAX',
            name='Foreign tax code',
        )
        foreign_row = TaxTransaction.objects.create(
            organization=self.other_organization,
            transaction_kind='sale',
            transaction_date=date(2026, 3, 1),
            source_model='SalesInvoice',
            source_id=303,
            tax_code=foreign_code,
        )
        own_row = TaxTransaction.objects.create(
            organization=self.organization,
            branch=self.branch,
            transaction_kind='sale',
            transaction_date=date(2026, 3, 1),
            source_model='SalesInvoice',
            source_id=101,
            tax_code=self.tax_code,
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse('tax_register_csv'),
            {
                'kind': 'sale',
                'branch': other_branch.pk,
                'organization': self.other_organization.pk,
                'start': '2026-03-01',
                'end': '2026-03-31',
                'q': 'foreign',
            },
        )

        contents = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'SalesInvoice-{own_row.source_id}', contents)
        self.assertNotIn(f'SalesInvoice-{other_branch_row.source_id}', contents)
        self.assertNotIn(f'SalesInvoice-{foreign_row.source_id}', contents)

        invalid_date_response = self.client.get(
            reverse('tax_register_csv'),
            {'start': 'not-a-date', 'organization': self.other_organization.pk},
        )
        self.assertEqual(invalid_date_response.status_code, 200)
        self.assertNotIn(f'SalesInvoice-{foreign_row.source_id}', invalid_date_response.content.decode())
