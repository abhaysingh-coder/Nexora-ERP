from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounting.models import JournalEntry, ensure_default_accounts
from core.models import User
from inventory.models import (
    InventoryValuationBalance,
    InventoryValuationEntry,
    Product,
    StockMovement,
    StockTransfer,
    Warehouse,
    adjust_stock,
    record_opening_valuation,
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
from sales.models import Customer, SalesInvoice, SalesInvoiceLine


class InventoryValuationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Valuation Co', slug='valuation-co')
        self.branch = Branch.objects.create(organization=self.organization, name='Main', code='MAIN')
        self.warehouse = Warehouse.objects.create(
            organization=self.organization, branch=self.branch, name='Main warehouse', code='MAIN-WH'
        )
        self.second_warehouse = Warehouse.objects.create(
            organization=self.organization, branch=self.branch, name='Second warehouse', code='SECOND-WH'
        )
        self.product = Product.objects.create(
            organization=self.organization,
            sku='VAL-1',
            name='Valuation item',
            cost_price=Decimal('999.00'),
            sales_price=Decimal('120.00'),
        )
        self.customer = Customer.objects.create(organization=self.organization, name='Valuation buyer')
        self.user = User.objects.create_user(
            username='valuation-manager',
            email='valuation@example.test',
            password='test-password',
            organization=self.organization,
            branch=self.branch,
        )
        OrganizationMembership.objects.create(user=self.user, organization=self.organization, role='owner')
        self.accounts = ensure_default_accounts(self.organization, self.branch)
        self.client.force_login(self.user)

    def receive(self, quantity, unit_cost, *, ref='OpeningReceipt', ref_id=1, warehouse=None):
        return adjust_stock(
            self.product,
            warehouse or self.warehouse,
            Decimal(quantity),
            'purchase',
            ref,
            ref_id,
            Decimal(unit_cost),
        )

    def test_perpetual_weighted_average_recalculates_receipts_and_costs_issues(self):
        self.receive('10', '10.00', ref_id=1)
        self.receive('10', '20.00', ref_id=2)
        balance = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity_on_hand, Decimal('20.000'))
        self.assertEqual(balance.inventory_value, Decimal('300.00'))
        self.assertEqual(balance.average_unit_cost, Decimal('15.000000'))

        sale = adjust_stock(
            self.product, self.warehouse, Decimal('-5'), 'sale', 'TestSale', 1, Decimal('999.00')
        )
        balance.refresh_from_db()
        self.assertEqual(sale.unit_cost, Decimal('15.000000'))
        self.assertEqual(sale.valuation_entry.value_change, Decimal('-75.00'))
        self.assertEqual(balance.quantity_on_hand, Decimal('15.000'))
        self.assertEqual(balance.inventory_value, Decimal('225.00'))

    def test_last_issue_clears_currency_rounding_residual(self):
        self.receive('3', '0.033333')
        first = adjust_stock(self.product, self.warehouse, Decimal('-1'), 'sale', 'RoundSale', 1)
        second = adjust_stock(self.product, self.warehouse, Decimal('-1'), 'sale', 'RoundSale', 2)
        last = adjust_stock(self.product, self.warehouse, Decimal('-1'), 'sale', 'RoundSale', 3)
        balance = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(
            [abs(first.valuation_entry.value_change), abs(second.valuation_entry.value_change), abs(last.valuation_entry.value_change)],
            [Decimal('0.03'), Decimal('0.04'), Decimal('0.03')],
        )
        self.assertEqual(balance.quantity_on_hand, Decimal('0.000'))
        self.assertEqual(balance.inventory_value, Decimal('0.00'))

    def test_transfer_carries_the_source_value_without_changing_company_total(self):
        self.receive('5', '10.01')
        source_before = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        transfer = StockTransfer.objects.create(
            organization=self.organization,
            reference='TR-VAL-1',
            transfer_date=date(2026, 10, 24),
            product=self.product,
            source_warehouse=self.warehouse,
            destination_warehouse=self.second_warehouse,
            quantity=Decimal('3'),
            created_by=self.user,
        )
        transfer.post()
        source_after = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        destination = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.second_warehouse)
        self.assertEqual(
            source_after.inventory_value + destination.inventory_value,
            source_before.inventory_value,
        )
        self.assertEqual(source_after.quantity_on_hand, Decimal('2.000'))
        self.assertEqual(destination.quantity_on_hand, Decimal('3.000'))
        self.assertEqual(destination.inventory_value, abs(
            StockMovement.objects.get(reference_model='StockTransfer', reference_id=transfer.pk, quantity__gt=0)
            .valuation_entry.value_change
        ))

    def test_sales_invoice_posts_cogs_from_warehouse_average_not_product_cost_price(self):
        self.receive('10', '10.00', ref_id=1)
        self.receive('10', '20.00', ref_id=2)
        invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            invoice_number='SI-VAL-1',
            invoice_date=date(2026, 10, 24),
            subtotal=Decimal('240.00'),
            total_amount=Decimal('240.00'),
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=Decimal('2'),
            unit_cost=Decimal('999.00'),
            unit_price=Decimal('120.00'),
            line_total=Decimal('240.00'),
        )
        invoice.post()
        movement = StockMovement.objects.get(reference_model='SalesInvoice', reference_id=invoice.pk)
        cogs = JournalEntry.objects.get(reference='SI-SI-VAL-1-cogs')
        self.assertEqual(movement.unit_cost, Decimal('15.000000'))
        self.assertEqual(cogs.lines.get(account=self.accounts['cost_of_goods_sold']).debit, Decimal('30.00'))
        self.assertEqual(cogs.lines.get(account=self.accounts['inventory']).credit, Decimal('30.00'))

    def test_receipt_is_valued_once_and_later_invoice_posts_purchase_price_variance(self):
        supplier = Supplier.objects.create(organization=self.organization, name='Valuation supplier')
        order = PurchaseOrder.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=supplier,
            order_number='PO-VAL-1',
            order_date=date(2026, 10, 24),
            created_by=self.user,
        )
        order_line = PurchaseOrderLine.objects.create(
            order=order,
            product=self.product,
            quantity=Decimal('10'),
            unit_cost=Decimal('10.00'),
        )
        order.confirm(user=self.user)
        receipt = GoodsReceipt.objects.create(
            organization=self.organization,
            branch=self.branch,
            warehouse=self.warehouse,
            order=order,
            receipt_number='GR-VAL-1',
            receipt_date=date(2026, 10, 24),
            created_by=self.user,
        )
        receipt_line = GoodsReceiptLine.objects.create(
            goods_receipt=receipt,
            order_line=order_line,
            quantity=Decimal('10'),
        )
        receipt.post()
        self.assertFalse(JournalEntry.objects.filter(reference=f'GRN-{receipt.receipt_number}').exists())
        self.assertEqual(
            InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse).inventory_value,
            Decimal('100.00'),
        )

        sales_invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=self.customer,
            invoice_number='SI-VAL-BEFORE-PI',
            invoice_date=date(2026, 10, 24),
            subtotal=Decimal('600.00'),
            total_amount=Decimal('600.00'),
        )
        SalesInvoiceLine.objects.create(
            invoice=sales_invoice,
            product=self.product,
            quantity=Decimal('5'),
            unit_cost=Decimal('999.00'),
            unit_price=Decimal('120.00'),
            line_total=Decimal('600.00'),
        )
        sales_invoice.post()
        self.assertEqual(
            InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse).inventory_value,
            Decimal('50.00'),
        )
        self.assertEqual(
            self.client.get(reverse('inventory_valuation')).context['reconciliation_difference'],
            Decimal('0.00'),
        )

        purchase_invoice = PurchaseInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=supplier,
            invoice_number='PI-VAL-1',
            invoice_date=date(2026, 10, 24),
            subtotal=Decimal('120.00'),
            total_amount=Decimal('120.00'),
        )
        PurchaseInvoiceLine.objects.create(
            invoice=purchase_invoice,
            product=self.product,
            receipt_line=receipt_line,
            purchase_order_line=order_line,
            quantity=Decimal('10'),
            unit_cost=Decimal('12.00'),
            line_total=Decimal('120.00'),
        )
        purchase_invoice.post()
        balance = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.product.refresh_from_db()
        self.assertEqual(balance.quantity_on_hand, Decimal('5.000'))
        self.assertEqual(balance.inventory_value, Decimal('60.00'))
        self.assertEqual(self.product.stock_quantity, Decimal('5.000'))
        purchase_entry = JournalEntry.objects.get(reference='PI-PI-VAL-1')
        self.assertEqual(
            purchase_entry.lines.get(account=self.accounts['cost_of_goods_sold']).debit,
            Decimal('10.00'),
        )
        self.assertEqual(
            self.client.get(reverse('inventory_valuation')).context['reconciliation_difference'],
            Decimal('0.00'),
        )

    def test_unvalued_legacy_position_blocks_issue_until_explicit_opening_value(self):
        movement = self.receive('5', '10.00')
        balance = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        balance.inventory_value = Decimal('0.00')
        balance.is_valued = False
        balance.valuation_start_date = None
        balance.save()
        with self.assertRaises(ValidationError):
            adjust_stock(self.product, self.warehouse, Decimal('-1'), 'sale', 'BlockedSale', 1)

        old_movement_count = StockMovement.objects.count()
        record_opening_valuation(
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('5'),
            inventory_value=Decimal('75.00'),
            user=self.user,
        )
        balance.refresh_from_db()
        self.assertEqual(StockMovement.objects.count(), old_movement_count)
        self.assertEqual(movement.unit_cost, Decimal('10.000000'))
        self.assertTrue(balance.is_valued)
        self.assertEqual(balance.inventory_value, Decimal('75.00'))
        self.assertEqual(balance.average_unit_cost, Decimal('15.000000'))

    def test_opening_valuation_allocates_unallocated_legacy_quantity_with_explicit_value(self):
        self.product.stock_quantity = Decimal('4.000')
        self.product.save(update_fields=['stock_quantity'])
        entry = record_opening_valuation(
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('4'),
            inventory_value=Decimal('52.00'),
            user=self.user,
        )
        self.product.refresh_from_db()
        balance = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(entry.event_type, 'opening')
        self.assertEqual(balance.quantity_on_hand, Decimal('4.000'))
        self.assertEqual(balance.inventory_value, Decimal('52.00'))
        self.assertEqual(self.product.stock_quantity, Decimal('4.000'))

    def test_receipts_can_enter_valued_warehouse_while_legacy_stock_stays_unallocated(self):
        self.product.stock_quantity = Decimal('4.000')
        self.product.save(update_fields=['stock_quantity'])

        receipt = self.receive('2', '10.00', ref='CurrentReceipt', ref_id=22)
        self.product.refresh_from_db()
        balance = InventoryValuationBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(receipt.valuation_entry.value_change, Decimal('20.00'))
        self.assertEqual(self.product.stock_quantity, Decimal('6.000'))
        self.assertEqual(balance.quantity_on_hand, Decimal('2.000'))
        with self.assertRaises(ValidationError):
            adjust_stock(self.product, self.warehouse, Decimal('-1'), 'sale', 'BlockedLegacySale', 1)

        record_opening_valuation(
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('6'),
            inventory_value=Decimal('80.00'),
            user=self.user,
        )
        balance.refresh_from_db()
        self.assertEqual(balance.quantity_on_hand, Decimal('6.000'))
        self.assertEqual(balance.inventory_value, Decimal('80.00'))
        self.assertEqual(self.product.stock_quantity, Decimal('6.000'))

    def test_movement_retry_is_idempotent_and_direct_ledger_writes_are_blocked(self):
        first = self.receive('2', '5.00', ref='RepeatReceipt', ref_id=17)
        retry = self.receive('2', '5.00', ref='RepeatReceipt', ref_id=17)
        self.assertEqual(first.pk, retry.pk)
        self.assertEqual(InventoryValuationEntry.objects.count(), 1)
        with self.assertRaises(ValidationError):
            StockMovement.objects.create(
                product=self.product,
                warehouse=self.warehouse,
                quantity=Decimal('1'),
                movement_type='purchase',
                reference_model='Bypass',
                reference_id=1,
            )
        with self.assertRaises(ValidationError):
            StockMovement.objects.filter(pk=first.pk).delete()

    def test_valuation_report_and_csv_are_scoped_and_show_gl_difference(self):
        self.receive('2', '12.50')
        response = self.client.get(reverse('inventory_valuation'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_value'], Decimal('25.00'))
        self.assertEqual(response.context['gl_inventory'], Decimal('0'))
        self.assertEqual(response.context['reconciliation_difference'], Decimal('25.00'))
        self.assertEqual(response.context['reconciliation_status'], 'Review required')
        csv_response = self.client.get(reverse('inventory_valuation'), {'format': 'csv'})
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn('Inventory value', csv_response.content.decode())

    def test_report_restricts_branch_users_to_their_branch(self):
        other_branch = Branch.objects.create(organization=self.organization, name='Other', code='OTHER')
        other_warehouse = Warehouse.objects.create(
            organization=self.organization, branch=other_branch, name='Other warehouse', code='OTHER-WH'
        )
        adjust_stock(self.product, other_warehouse, Decimal('2'), 'purchase', 'OtherReceipt', 1, Decimal('50'))
        response = self.client.get(reverse('inventory_valuation'))
        self.assertNotContains(response, 'OTHER-WH')
        self.assertContains(response, 'MAIN-WH')

    def test_both_valuation_csv_modes_ignore_tampered_branch_and_reject_foreign_warehouses(self):
        self.receive('2', '12.50', ref_id=31)
        other_branch = Branch.objects.create(
            organization=self.organization,
            name='Other branch',
            code='OTHER',
        )
        other_warehouse = Warehouse.objects.create(
            organization=self.organization,
            branch=other_branch,
            name='Other warehouse',
            code='OTHER-WH',
        )
        foreign_organization = Organization.objects.create(
            name='Foreign valuation tenant',
            slug='foreign-valuation-tenant',
        )
        foreign_branch = Branch.objects.create(
            organization=foreign_organization,
            name='Foreign branch',
            code='FOREIGN',
        )
        foreign_warehouse = Warehouse.objects.create(
            organization=foreign_organization,
            branch=foreign_branch,
            name='Foreign warehouse',
            code='FOREIGN-WH',
        )
        foreign_product = Product.objects.create(
            organization=foreign_organization,
            sku='FOREIGN-VAL',
            name='Foreign valuation item',
            cost_price=Decimal('5.00'),
            sales_price=Decimal('8.00'),
        )
        adjust_stock(
            self.product,
            other_warehouse,
            Decimal('3'),
            'purchase',
            'OtherBranchCsv',
            1,
            Decimal('20.00'),
        )
        adjust_stock(
            foreign_product,
            foreign_warehouse,
            Decimal('4'),
            'purchase',
            'ForeignTenantCsv',
            1,
            Decimal('30.00'),
        )

        for export_filter in ({'format': 'csv'}, {'history_csv': '1'}):
            with self.subTest(export_filter=export_filter):
                response = self.client.get(
                    reverse('inventory_valuation'),
                    {**export_filter, 'branch': other_branch.pk},
                )
                self.assertEqual(response.status_code, 200)
                contents = response.content.decode()
                self.assertIn('MAIN-WH', contents)
                self.assertNotIn('OTHER-WH', contents)
                self.assertNotIn('FOREIGN-WH', contents)
                self.assertNotIn('FOREIGN-VAL', contents)

                for warehouse in (other_warehouse, foreign_warehouse):
                    with self.subTest(export_filter=export_filter, warehouse=warehouse.code):
                        scoped_response = self.client.get(
                            reverse('inventory_valuation'),
                            {**export_filter, 'warehouse': warehouse.pk},
                        )
                        self.assertEqual(scoped_response.status_code, 404)
