from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounting.models import JournalEntry, ensure_default_accounts
from inventory.models import Product, StockMovement, Warehouse, adjust_stock
from organizations.models import Branch, Organization
from purchases.models import PurchaseInvoice, PurchaseInvoiceLine, Supplier
from sales.models import Customer, CustomerReceipt, SalesInvoice, SalesInvoiceLine


class ERPVerticalSliceTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Nexa Labs', slug='nexa-labs', currency='INR')
        self.branch = Branch.objects.create(organization=self.organization, name='Mumbai', code='MUM')
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            branch=self.branch,
            name='Main Warehouse',
            code='WH-01',
        )
        self.supplier = Supplier.objects.create(
            organization=self.organization,
            name='Prime Suppliers',
            gstin='27AAAAA0000A1Z5',
        )
        self.customer = Customer.objects.create(
            organization=self.organization,
            name='Blue Cart Retail',
            gstin='27BBBBB0000A1Z5',
        )
        self.product = Product.objects.create(
            organization=self.organization,
            sku='SKU-1001',
            name='Industrial Switch',
            category='Electronics',
            unit='pcs',
            cost_price=Decimal('100.00'),
            sales_price=Decimal('150.00'),
            reorder_level=Decimal('5.00'),
            stock_quantity=Decimal('0.00'),
        )
        self.accounts = ensure_default_accounts(self.organization, self.branch)

    def test_purchase_sales_receipt_cycle_updates_stock_and_accounts(self):
        purchase_invoice = PurchaseInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=self.supplier,
            invoice_number='PI-1001',
            invoice_date='2026-01-02',
            due_date='2026-01-30',
            subtotal=Decimal('1000.00'),
            tax_amount=Decimal('180.00'),
            total_amount=Decimal('1180.00'),
        )
        PurchaseInvoiceLine.objects.create(
            invoice=purchase_invoice,
            product=self.product,
            quantity=Decimal('10'),
            unit_cost=Decimal('100.00'),
            tax_rate=Decimal('18.00'),
            line_total=Decimal('1000.00'),
        )

        purchase_invoice.post()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('10.000'))
        self.assertEqual(purchase_invoice.status, 'posted')
        self.assertTrue(JournalEntry.objects.filter(reference='PI-PI-1001').exists())

        sales_invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            invoice_number='SI-8001',
            invoice_date='2026-01-05',
            due_date='2026-01-25',
            subtotal=Decimal('750.00'),
            tax_amount=Decimal('135.00'),
            total_amount=Decimal('885.00'),
        )
        SalesInvoiceLine.objects.create(
            invoice=sales_invoice,
            product=self.product,
            quantity=Decimal('5'),
            unit_cost=Decimal('100.00'),
            unit_price=Decimal('150.00'),
            tax_rate=Decimal('18.00'),
            line_total=Decimal('750.00'),
        )

        sales_invoice.post()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('5.000'))
        self.assertEqual(sales_invoice.status, 'posted')

        receipt = CustomerReceipt.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            receipt_number='CR-1002',
            receipt_date='2026-01-06',
            amount=Decimal('885.00'),
            payment_method='bank',
        )
        receipt.post()
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_balance, Decimal('0.00'))
        self.assertEqual(receipt.status, 'posted')

    def test_rejects_invalid_invoice_totals(self):
        invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            invoice_number='SI-8002',
            invoice_date='2026-01-08',
            subtotal=Decimal('500.00'),
            tax_amount=Decimal('100.00'),
            total_amount=Decimal('700.00'),
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=Decimal('2'),
            unit_cost=Decimal('100.00'),
            unit_price=Decimal('150.00'),
            tax_rate=Decimal('18.00'),
            line_total=Decimal('200.00'),
        )

        with self.assertRaises(ValidationError):
            invoice.post()

    def test_duplicate_stock_adjustment_is_idempotent(self):
        first = adjust_stock(self.product, self.warehouse, Decimal('3'), 'purchase', 'PurchaseInvoice', 101, Decimal('42.00'))
        second = adjust_stock(self.product, self.warehouse, Decimal('3'), 'purchase', 'PurchaseInvoice', 101, Decimal('42.00'))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(StockMovement.objects.filter(reference_model='PurchaseInvoice', reference_id=101).count(), 1)
