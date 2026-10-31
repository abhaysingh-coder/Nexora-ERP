from datetime import date
from decimal import Decimal
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounting.models import (
    Account,
    AccountingPeriod,
    FinancialYear,
    JournalEntry,
    ensure_default_accounts,
)
from core.models import AuditEvent, User
from banking.services import invoice_open_amount
from inventory.models import (
    InventoryValuationBalance,
    Product,
    StockMovement,
    Warehouse,
    adjust_stock,
    warehouse_stock_balance,
)
from organizations.models import Branch, Organization, OrganizationMembership
from purchases.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
)
from returns.models import (
    CustomerReturn,
    CustomerReturnLine,
    SalesCreditNote,
    SupplierDebitNote,
    SupplierReturn,
    SupplierReturnLine,
)
from sales.models import Customer, SalesInvoice, SalesInvoiceLine
from taxation.models import ProductTaxClassification, TaxCode, TaxRate, TaxRegistration, TaxTransaction


class ReturnWorkflowTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Returns Co', slug='returns-co')
        self.branch = Branch.objects.create(organization=self.organization, name='Main', code='MAIN')
        self.warehouse = Warehouse.objects.create(
            organization=self.organization, branch=self.branch, name='Main warehouse', code='MAIN-WH'
        )
        self.product = Product.objects.create(
            organization=self.organization,
            sku='RETURN-1',
            name='Returnable item',
            cost_price=Decimal('40.00'),
            sales_price=Decimal('100.00'),
        )
        self.customer = Customer.objects.create(organization=self.organization, name='Return customer', state_code='27')
        self.supplier = Supplier.objects.create(organization=self.organization, name='Return supplier', state_code='27')
        self.user = User.objects.create_user(
            username='return-manager', email='return-manager@example.test', password='test-password',
            organization=self.organization, branch=self.branch,
        )
        OrganizationMembership.objects.create(user=self.user, organization=self.organization, role='owner')
        self.accounts = ensure_default_accounts(self.organization, self.branch)
        adjust_stock(self.product, self.warehouse, Decimal('20'), 'purchase', 'OpeningStock', 1, Decimal('40'))
        self.client.force_login(self.user)

    def sales_invoice(self, *, number='SI-RET-1', quantity=Decimal('5'), unit_price=Decimal('100'), tax=False):
        if tax:
            self.configure_gst()
        invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            invoice_number=number,
            invoice_date=date(2026, 10, 1),
            place_of_supply='27' if tax else '',
            subtotal=quantity * unit_price,
            total_amount=quantity * unit_price,
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=quantity,
            unit_cost=Decimal('40'),
            unit_price=unit_price,
            line_total=quantity * unit_price,
        )
        invoice.post()
        invoice.refresh_from_db()
        return invoice

    def purchase_invoice(self, *, number='PI-RET-1', quantity=Decimal('5'), unit_cost=Decimal('50'), tax=False):
        if tax:
            self.configure_gst()
        invoice = PurchaseInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=self.supplier,
            invoice_number=number,
            invoice_date=date(2026, 10, 1),
            place_of_supply='27' if tax else '',
            subtotal=quantity * unit_cost,
            total_amount=quantity * unit_cost,
        )
        PurchaseInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=quantity,
            unit_cost=unit_cost,
            line_total=quantity * unit_cost,
        )
        invoice.post()
        invoice.refresh_from_db()
        return invoice

    def configure_gst(self):
        code, _ = TaxCode.objects.get_or_create(
            organization=self.organization,
            code='GST18',
            defaults={'name': 'GST 18%'},
        )
        for tax_type, rate in (('cgst', '9'), ('sgst', '9'), ('igst', '18')):
            TaxRate.objects.get_or_create(
                organization=self.organization,
                tax_code=code,
                tax_type=tax_type,
                state_code='',
                effective_from=date(2026, 1, 1),
                defaults={'rate': Decimal(rate)},
            )
        TaxRegistration.objects.get_or_create(
            organization=self.organization,
            branch=self.branch,
            defaults={'gstin': '27AAAAA0000A1Z5', 'state_code': '27', 'is_registered': True},
        )
        ProductTaxClassification.objects.get_or_create(
            organization=self.organization,
            product=self.product,
            defaults={'tax_code': code},
        )
        return code

    def customer_return(self, invoice, *, number, quantity, restock=True, condition='good', return_date=date(2026, 10, 2)):
        record = CustomerReturn.objects.create(
            organization=self.organization,
            branch=invoice.branch,
            invoice=invoice,
            return_number=number,
            return_date=return_date,
            warehouse=self.warehouse if restock else None,
            created_by=self.user,
        )
        CustomerReturnLine.objects.create(
            customer_return=record,
            invoice_line=invoice.lines.get(),
            quantity=quantity,
            reason='Customer return',
            condition=condition,
            restock=restock,
        )
        return record

    def supplier_return_from_invoice(self, invoice, *, number, quantity, return_date=date(2026, 10, 2)):
        invoice_line = invoice.lines.get()
        record = SupplierReturn.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=self.supplier,
            purchase_invoice=invoice,
            warehouse=self.warehouse,
            return_number=number,
            return_date=return_date,
            created_by=self.user,
        )
        SupplierReturnLine.objects.create(
            supplier_return=record,
            invoice_line=invoice_line,
            quantity=quantity,
            reason='Supplier return',
            condition='good',
        )
        return record

    def test_customer_partial_and_multiple_returns_create_credit_notes_and_restore_only_sellable_stock(self):
        invoice = self.sales_invoice(quantity=Decimal('5'))
        first = self.customer_return(invoice, number='CR-RET-1', quantity=Decimal('2'), restock=True)
        first.post(user=self.user)
        self.product.refresh_from_db()
        self.customer.refresh_from_db()
        note = SalesCreditNote.objects.get(customer_return=first)
        self.assertEqual(note.total_amount, Decimal('200.00'))
        self.assertEqual(self.product.stock_quantity, Decimal('17.000'))
        self.assertEqual(warehouse_stock_balance(self.product, self.warehouse), Decimal('17.000'))
        valuation = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(valuation.inventory_value, Decimal('680.00'))
        self.assertEqual(self.customer.outstanding_balance, Decimal('300.00'))
        self.assertEqual(invoice_open_amount(invoice), Decimal('300.00'))
        self.assertTrue(note.journal_entry.is_balanced())
        self.assertEqual(StockMovement.objects.filter(reference_model='CustomerReturn', reference_id=first.pk).count(), 1)

        # Posting the same request again is idempotent.
        first.post(user=self.user)
        self.assertEqual(SalesCreditNote.objects.filter(customer_return=first).count(), 1)
        self.assertEqual(StockMovement.objects.filter(reference_model='CustomerReturn', reference_id=first.pk).count(), 1)

        second = self.customer_return(
            invoice, number='CR-RET-2', quantity=Decimal('3'), restock=False,
            condition='damaged',
        )
        second.post(user=self.user)
        self.product.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(SalesCreditNote.objects.get(customer_return=second).total_amount, Decimal('300.00'))
        self.assertEqual(self.product.stock_quantity, Decimal('17.000'))
        self.assertEqual(self.customer.outstanding_balance, Decimal('0.00'))
        self.assertEqual(invoice_open_amount(invoice), Decimal('0.00'))
        self.assertFalse(StockMovement.objects.filter(reference_model='CustomerReturn', reference_id=second.pk).exists())
        with self.assertRaises(ValidationError):
            self.customer_return(invoice, number='CR-RET-OVER', quantity=Decimal('0.001')).post(user=self.user)
        self.assertEqual(invoice.status, 'posted')
        self.assertEqual(AuditEvent.objects.filter(action='customer_return_posted').count(), 2)

    def test_customer_credit_note_reverses_stored_gst_snapshot_and_rounds_partial_returns_cumulatively(self):
        invoice = self.sales_invoice(number='SI-GST-RET', quantity=Decimal('3'), tax=True)
        original = TaxTransaction.objects.get(source_model='SalesInvoice', source_id=invoice.pk)
        first = self.customer_return(invoice, number='CR-GST-1', quantity=Decimal('1'), restock=False)
        first.post(user=self.user)
        first_note = SalesCreditNote.objects.get(customer_return=first)
        first_tax = TaxTransaction.objects.get(source_model='SalesCreditNote', source_id=first_note.pk)
        self.assertEqual(first_note.subtotal, Decimal('100.00'))
        self.assertEqual(first_note.tax_amount, Decimal('18.00'))
        self.assertEqual(first_note.total_amount, Decimal('118.00'))
        self.assertEqual(first_tax.taxable_value, Decimal('-100.00'))
        self.assertEqual(first_tax.cgst, Decimal('-9.00'))
        self.assertEqual(first_tax.sgst, Decimal('-9.00'))
        self.assertEqual(first_tax.total_tax, Decimal('-18.00'))
        self.assertEqual(first_note.journal_entry.total_debit, first_note.journal_entry.total_credit)

        second = self.customer_return(invoice, number='CR-GST-2', quantity=Decimal('2'), restock=False)
        second.post(user=self.user)
        second_note = SalesCreditNote.objects.get(customer_return=second)
        second_tax = TaxTransaction.objects.get(source_model='SalesCreditNote', source_id=second_note.pk)
        self.assertEqual(second_note.total_amount, Decimal('236.00'))
        self.assertEqual(second_tax.taxable_value, Decimal('-200.00'))
        self.assertEqual(second_tax.total_tax, Decimal('-36.00'))
        self.assertEqual(TaxTransaction.objects.get(pk=original.pk).total_tax, Decimal('54.00'))

    def test_supplier_invoice_return_posts_debit_note_reverses_payable_tax_and_stock_once(self):
        invoice = self.purchase_invoice(quantity=Decimal('4'), unit_cost=Decimal('50'), tax=True)
        self.assertEqual(invoice.total_amount, Decimal('236.00'))
        record = self.supplier_return_from_invoice(invoice, number='SR-GST-1', quantity=Decimal('2'))
        record.post(user=self.user)
        record.post(user=self.user)
        note = SupplierDebitNote.objects.get(supplier_return=record)
        reversal = TaxTransaction.objects.get(source_model='SupplierDebitNote', source_id=note.pk)
        self.product.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertEqual(note.total_amount, Decimal('118.00'))
        self.assertEqual(note.tax_amount, Decimal('18.00'))
        self.assertEqual(reversal.taxable_value, Decimal('-100.00'))
        self.assertEqual(reversal.cgst, Decimal('-9.00'))
        self.assertEqual(reversal.sgst, Decimal('-9.00'))
        self.assertEqual(note.journal_entry.total_debit, note.journal_entry.total_credit)
        self.assertEqual(note.journal_entry.lines.get(account=self.accounts['accounts_payable']).debit, Decimal('118.00'))
        self.assertEqual(note.journal_entry.lines.get(account=self.accounts['gst_input']).credit, Decimal('18.00'))
        self.assertEqual(self.product.stock_quantity, Decimal('22.000'))
        valuation = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(valuation.inventory_value, Decimal('916.67'))
        self.assertEqual(record.lines.get().stock_cost, Decimal('83.33'))
        self.assertEqual(
            note.journal_entry.lines.get(account=self.accounts['inventory_adjustment_gain']).credit,
            Decimal('16.67'),
        )
        self.assertEqual(self.supplier.outstanding_balance, Decimal('118.00'))
        self.assertEqual(invoice_open_amount(invoice), Decimal('118.00'))
        self.assertEqual(StockMovement.objects.filter(reference_model='SupplierReturn', reference_id=record.pk).count(), 1)

    def test_supplier_partial_returns_accumulate_and_reduce_the_original_invoice_open_amount(self):
        invoice = self.purchase_invoice(number='PI-MULTI-RET', quantity=Decimal('3'), unit_cost=Decimal('25'))
        first = self.supplier_return_from_invoice(invoice, number='SR-MULTI-1', quantity=Decimal('1'))
        first.post(user=self.user)
        second = self.supplier_return_from_invoice(invoice, number='SR-MULTI-2', quantity=Decimal('1'))
        second.post(user=self.user)
        self.assertEqual(SupplierDebitNote.objects.get(supplier_return=first).total_amount, Decimal('25.00'))
        self.assertEqual(SupplierDebitNote.objects.get(supplier_return=second).total_amount, Decimal('25.00'))
        self.assertEqual(invoice_open_amount(invoice), Decimal('25.00'))
        last = self.supplier_return_from_invoice(invoice, number='SR-MULTI-3', quantity=Decimal('1'))
        last.post(user=self.user)
        self.assertEqual(invoice_open_amount(invoice), Decimal('0.00'))
        over = self.supplier_return_from_invoice(invoice, number='SR-MULTI-OVER', quantity=Decimal('0.001'))
        with self.assertRaises(ValidationError):
            over.post(user=self.user)
        self.assertEqual(SupplierDebitNote.objects.filter(supplier_return__purchase_invoice=invoice).count(), 3)

    def test_partial_credit_rounding_uses_cumulative_original_line_snapshot(self):
        invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            invoice_number='SI-ROUND-RET',
            invoice_date=date(2026, 10, 1),
            subtotal=Decimal('0.10'),
            total_amount=Decimal('0.10'),
        )
        invoice_line = SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=Decimal('3'),
            unit_cost=Decimal('0.01'),
            unit_price=Decimal('0.10'),
            discount_amount=Decimal('0.20'),
            line_total=Decimal('0.30'),
        )
        invoice.post()
        amounts = []
        for index in range(1, 4):
            record = self.customer_return(invoice, number=f'CR-ROUND-{index}', quantity=Decimal('1'), restock=False)
            record.post(user=self.user)
            amounts.append(SalesCreditNote.objects.get(customer_return=record).total_amount)
        self.assertEqual(amounts, [Decimal('0.03'), Decimal('0.04'), Decimal('0.03')])
        self.assertEqual(sum(amounts, Decimal('0')), Decimal('0.10'))

    def test_reverse_charge_debit_note_reverses_both_rcm_sides(self):
        self.configure_gst()
        classification = ProductTaxClassification.objects.get(organization=self.organization, product=self.product)
        classification.is_reverse_charge = True
        classification.save()
        self.supplier.is_reverse_charge_applicable = True
        self.supplier.save()
        invoice = self.purchase_invoice(number='PI-RCM-RET', quantity=Decimal('2'), unit_cost=Decimal('100'), tax=True)
        source_tax = TaxTransaction.objects.get(source_model='PurchaseInvoice', source_id=invoice.pk)
        self.assertTrue(source_tax.is_reverse_charge)
        self.assertEqual(invoice.total_amount, Decimal('200.00'))
        record = self.supplier_return_from_invoice(invoice, number='SR-RCM-1', quantity=Decimal('1'))
        record.post(user=self.user)
        note = SupplierDebitNote.objects.get(supplier_return=record)
        reversal = TaxTransaction.objects.get(source_model='SupplierDebitNote', source_id=note.pk)
        self.assertEqual(note.total_amount, Decimal('100.00'))
        self.assertEqual(reversal.total_tax, Decimal('-18.00'))
        self.assertTrue(reversal.is_reverse_charge)
        self.assertEqual(note.journal_entry.lines.get(account=self.accounts['gst_payable']).debit, Decimal('18.00'))
        self.assertEqual(note.journal_entry.lines.get(account=self.accounts['gst_input']).credit, Decimal('18.00'))
        self.assertEqual(note.journal_entry.total_debit, note.journal_entry.total_credit)
        self.client.force_login(self.user)
        liability = self.client.get(reverse('tax_liability_summary'))
        self.assertEqual(liability.status_code, 200)
        self.assertEqual(liability.context['output_tax'], Decimal('18.00'))
        self.assertEqual(liability.context['input_tax'], Decimal('18.00'))

    def test_receipt_only_supplier_return_records_supplier_credit_and_cannot_exceed_unbilled_received_quantity(self):
        order = PurchaseOrder.objects.create(
            organization=self.organization, branch=self.branch, supplier=self.supplier,
            order_number='PO-RET-1', order_date=date(2026, 9, 30), created_by=self.user,
        )
        order_line = PurchaseOrderLine.objects.create(
            order=order, product=self.product, quantity=Decimal('3'), unit_cost=Decimal('50'),
        )
        order.confirm(user=self.user)
        receipt = GoodsReceipt.objects.create(
            organization=self.organization, branch=self.branch, warehouse=self.warehouse,
            order=order, receipt_number='GR-RET-1', receipt_date=date(2026, 10, 1), created_by=self.user,
        )
        receipt_line = GoodsReceiptLine.objects.create(
            goods_receipt=receipt, order_line=order_line, quantity=Decimal('3'),
        )
        receipt.post()
        record = SupplierReturn.objects.create(
            organization=self.organization, branch=self.branch, supplier=self.supplier,
            goods_receipt=receipt, warehouse=self.warehouse, return_number='SR-GRN-1',
            return_date=date(2026, 10, 2), created_by=self.user,
        )
        SupplierReturnLine.objects.create(
            supplier_return=record, receipt_line=receipt_line, quantity=Decimal('2'),
            reason='Excess goods', condition='good',
        )
        record.post(user=self.user)
        note = SupplierDebitNote.objects.get(supplier_return=record)
        self.product.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertEqual(note.total_amount, Decimal('100.00'))
        self.assertEqual(note.journal_entry.total_debit, note.journal_entry.total_credit)
        self.assertEqual(self.product.stock_quantity, Decimal('21.000'))
        self.assertEqual(self.supplier.outstanding_balance, Decimal('-100.00'))
        self.assertEqual(warehouse_stock_balance(self.product, self.warehouse), Decimal('21.000'))

        too_many = SupplierReturn.objects.create(
            organization=self.organization, branch=self.branch, supplier=self.supplier,
            goods_receipt=receipt, warehouse=self.warehouse, return_number='SR-GRN-OVER',
            return_date=date(2026, 10, 3), created_by=self.user,
        )
        SupplierReturnLine.objects.create(
            supplier_return=too_many, receipt_line=receipt_line, quantity=Decimal('2'),
            reason='Extra', condition='good',
        )
        with self.assertRaises(ValidationError):
            too_many.post(user=self.user)
        self.assertFalse(SupplierDebitNote.objects.filter(supplier_return=too_many).exists())

    def test_supplier_return_rejects_stock_below_zero_and_over_returned_invoice_quantity(self):
        invoice = self.purchase_invoice(number='PI-STOCK-RET', quantity=Decimal('2'), unit_cost=Decimal('50'))
        sale = self.sales_invoice(number='SI-CONSUME-RET', quantity=Decimal('22'), unit_price=Decimal('100'))
        record = self.supplier_return_from_invoice(invoice, number='SR-NOSTOCK', quantity=Decimal('1'))
        with self.assertRaises(ValidationError):
            record.post(user=self.user)
        self.assertFalse(SupplierDebitNote.objects.filter(supplier_return=record).exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('0.000'))
        self.assertEqual(sale.status, 'posted')

    def test_closed_period_blocks_return_posting_and_keeps_draft(self):
        invoice = self.sales_invoice(number='SI-CLOSED-RET', quantity=Decimal('1'))
        record = self.customer_return(invoice, number='CR-CLOSED', quantity=Decimal('1'))
        year = FinancialYear.objects.create(
            organization=self.organization, name='FY 2026', starts_on=date(2026, 1, 1), ends_on=date(2026, 12, 31)
        )
        period = AccountingPeriod.objects.create(
            financial_year=year, name='October', starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31)
        )
        period.close(user=self.user)
        with self.assertRaises(ValidationError):
            record.post(user=self.user)
        record.refresh_from_db()
        self.assertEqual(record.status, 'draft')
        self.assertFalse(SalesCreditNote.objects.filter(customer_return=record).exists())

    def test_return_screens_enforce_scope_permissions_and_complete_posting_actions(self):
        invoice = self.sales_invoice(number='SI-SCREEN-RET', quantity=Decimal('2'))
        self.assertEqual(self.client.get(reverse('customer_return_list')).status_code, 200)
        response = self.client.get(reverse('customer_return_create', args=[invoice.pk]))
        self.assertEqual(response.status_code, 200)
        payload = {
            'return_number': 'CR-SCREEN-1',
            'return_date': '2026-10-02',
            'reference': 'RMA-1',
            'notes': 'Box intact',
            'warehouse': str(self.warehouse.pk),
            f'quantity_{invoice.lines.get().pk}': '1',
            f'reason_{invoice.lines.get().pk}': 'Wrong item',
            f'condition_{invoice.lines.get().pk}': 'good',
            f'restock_{invoice.lines.get().pk}': 'on',
        }
        response = self.client.post(reverse('customer_return_create', args=[invoice.pk]), payload)
        self.assertEqual(response.status_code, 302)
        record = CustomerReturn.objects.get(return_number='CR-SCREEN-1')
        self.assertEqual(self.client.get(reverse('customer_return_detail', args=[record.pk])).status_code, 200)
        response = self.client.post(reverse('customer_return_detail', args=[record.pk]), {'action': 'post'})
        self.assertRedirects(response, reverse('customer_return_detail', args=[record.pk]))
        response = self.client.get(reverse('customer_return_list'))
        self.assertContains(response, 'CN-CR-SCREEN-1')

        other = Organization.objects.create(name='Other returns', slug='other-returns')
        other_customer = Customer.objects.create(organization=other, name='Other')
        foreign_invoice = SalesInvoice.objects.create(
            organization=other, customer=other_customer, invoice_number='SI-FOREIGN-RET', invoice_date=date(2026, 10, 1)
        )
        self.assertEqual(self.client.get(reverse('customer_return_create', args=[foreign_invoice.pk])).status_code, 404)
        ordinary = User.objects.create_user(
            username='return-staff', email='return-staff@example.test', password='test-password',
            organization=self.organization, branch=self.branch,
        )
        OrganizationMembership.objects.create(user=ordinary, organization=self.organization, role='staff')
        self.client.force_login(ordinary)
        self.assertEqual(self.client.get(reverse('customer_return_create', args=[invoice.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('customer_return_detail', args=[record.pk]), {'action': 'cancel'}).status_code, 403)
        draft = self.customer_return(invoice, number='CR-UNAUTHORIZED', quantity=Decimal('1'), restock=False)
        self.assertEqual(self.client.post(reverse('customer_return_detail', args=[draft.pk]), {'action': 'post'}).status_code, 403)

    def test_posted_return_records_and_original_documents_cannot_be_rewritten(self):
        invoice = self.sales_invoice(number='SI-IMMUTABLE-RET', quantity=Decimal('1'))
        record = self.customer_return(invoice, number='CR-IMMUTABLE', quantity=Decimal('1'))
        record.post(user=self.user)
        line = record.lines.get()
        with self.assertRaises(ValidationError):
            line.quantity = Decimal('0.5')
            line.save()
        with self.assertRaises(ValidationError):
            CustomerReturn.objects.filter(pk=record.pk).update(status='draft')
        with self.assertRaises(ValidationError):
            CustomerReturnLine.objects.filter(pk=line.pk).update(quantity=Decimal('0.5'))
        with self.assertRaises(ValidationError):
            record.delete()
        note = SalesCreditNote.objects.get(customer_return=record)
        with self.assertRaises(ValidationError):
            note.total_amount = Decimal('1')
            note.save()
        with self.assertRaises(ValidationError):
            SalesCreditNote.objects.filter(pk=note.pk).update(total_amount=Decimal('1'))
        with self.assertRaises(ValidationError):
            note.lines.update(quantity=Decimal('0.5'))
        with self.assertRaises(ValidationError):
            StockMovement.objects.filter(reference_model='CustomerReturn', reference_id=record.pk).delete()

    def test_draft_edit_cancel_and_supplier_ui_routes_work(self):
        invoice = self.sales_invoice(number='SI-DRAFT-RET', quantity=Decimal('1'))
        record = self.customer_return(invoice, number='CR-DRAFT-EDIT', quantity=Decimal('1'))
        response = self.client.get(reverse('customer_return_edit', args=[record.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post(reverse('customer_return_detail', args=[record.pk]), {'action': 'cancel'}).status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.status, 'cancelled')

        purchase = self.purchase_invoice(number='PI-SUPPLIER-UI', quantity=Decimal('1'), unit_cost=Decimal('25'))
        response = self.client.get(reverse('supplier_return_list'))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(reverse('supplier_return_create_from_invoice', args=[purchase.pk]))
        self.assertEqual(response.status_code, 200)
        line = purchase.lines.get()
        response = self.client.post(reverse('supplier_return_create_from_invoice', args=[purchase.pk]), {
            'return_number': 'SR-UI-1',
            'return_date': '2026-10-02',
            'reference': '',
            'notes': '',
            'warehouse': str(self.warehouse.pk),
            f'quantity_{line.pk}': '1',
            f'reason_{line.pk}': 'Wrong size',
            f'condition_{line.pk}': 'good',
        })
        self.assertEqual(response.status_code, 302)
        record = SupplierReturn.objects.get(return_number='SR-UI-1')
        self.assertEqual(self.client.get(reverse('supplier_return_detail', args=[record.pk])).status_code, 200)
        response = self.client.post(reverse('supplier_return_detail', args=[record.pk]), {'action': 'post'})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(reverse('supplier_return_list')), 'DN-SR-UI-1')


class ConcurrentReturnRetryTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_customer_return_retries_do_not_duplicate_note_journal_or_stock(self):
        organization = Organization.objects.create(name='Concurrent Co', slug='concurrent-returns')
        branch = Branch.objects.create(organization=organization, name='Main', code='MAIN')
        warehouse = Warehouse.objects.create(organization=organization, branch=branch, name='Main', code='MAIN-WH')
        product = Product.objects.create(organization=organization, sku='CONCURRENT-1', name='Concurrent item')
        customer = Customer.objects.create(organization=organization, name='Concurrent customer')
        accounts = ensure_default_accounts(organization, branch)
        adjust_stock(product, warehouse, Decimal('3'), 'purchase', 'OpeningConcurrent', 1, Decimal('10'))
        invoice = SalesInvoice.objects.create(
            organization=organization, branch=branch, customer=customer, invoice_number='SI-CONCURRENT',
            invoice_date=date(2026, 10, 1), subtotal=Decimal('100'), total_amount=Decimal('100'),
        )
        invoice_line = SalesInvoiceLine.objects.create(
            invoice=invoice, product=product, quantity=Decimal('1'), unit_cost=Decimal('10'),
            unit_price=Decimal('100'), line_total=Decimal('100'),
        )
        invoice.post()
        record = CustomerReturn.objects.create(
            organization=organization, branch=branch, invoice=invoice, return_number='CR-CONCURRENT',
            return_date=date(2026, 10, 2), warehouse=warehouse,
        )
        CustomerReturnLine.objects.create(
            customer_return=record, invoice_line=invoice_line, quantity=Decimal('1'),
            reason='Retry test', condition='good', restock=True,
        )
        barrier = Barrier(2)

        def worker():
            close_old_connections()
            try:
                retry = CustomerReturn.objects.get(pk=record.pk)
                barrier.wait(timeout=5)
                retry.post()
                return 'posted'
            except OperationalError as exc:
                if 'locked' in str(exc).lower():
                    return 'sqlite-lock-retry'
                raise
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: worker(), range(2)))
        record.refresh_from_db()
        product.refresh_from_db()
        self.assertIn('posted', outcomes)
        self.assertEqual(record.status, 'posted')
        self.assertEqual(SalesCreditNote.objects.filter(customer_return=record).count(), 1)
        self.assertEqual(JournalEntry.objects.filter(reference='CN-CN-CR-CONCURRENT').count(), 1)
        self.assertEqual(StockMovement.objects.filter(reference_model='CustomerReturn', reference_id=record.pk).count(), 1)
        self.assertEqual(product.stock_quantity, Decimal('3.000'))
        self.assertTrue(accounts['accounts_receivable'].journal_lines.filter(entry__reference='CN-CN-CR-CONCURRENT').exists())

