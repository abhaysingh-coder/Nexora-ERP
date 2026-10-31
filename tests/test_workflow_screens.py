from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from accounting.models import JournalEntry, ensure_default_accounts
from core.models import AuditEvent, User
from inventory.models import (
    Product,
    StockMovement,
    StockTransfer,
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
from sales.models import (
    Customer,
    DeliveryNote,
    DeliveryNoteLine,
    SalesInvoice,
    SalesOrder,
    SalesOrderLine,
)


class PurchaseOrderWorkflowTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Screen Co', slug='screen-co')
        self.branch = Branch.objects.create(
            organization=self.organization,
            name='Main',
            code='MAIN',
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            branch=self.branch,
            name='Main warehouse',
            code='WH1',
        )
        self.product = Product.objects.create(
            organization=self.organization,
            sku='SCREEN-1',
            name='Screen product',
            cost_price=Decimal('10.00'),
            sales_price=Decimal('15.00'),
            reorder_level=Decimal('2.000'),
        )
        self.customer = Customer.objects.create(
            organization=self.organization,
            name='Screen customer',
        )
        self.supplier = Supplier.objects.create(
            organization=self.organization,
            name='Screen supplier',
        )
        self.user = User.objects.create_user(
            username='screen-manager',
            email='screen-manager@example.test',
            password='test-password',
            organization=self.organization,
            branch=self.branch,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='owner',
            is_active=True,
        )
        ensure_default_accounts(self.organization, self.branch)
        self.client.force_login(self.user)


    def _order(self, *, quantity=Decimal('5.000'), number='PO-1'):
        order = PurchaseOrder.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=self.supplier,
            order_number=number,
            order_date='2026-10-01',
            created_by=self.user,
        )
        line = PurchaseOrderLine.objects.create(
            order=order,
            product=self.product,
            quantity=quantity,
            unit_cost=Decimal('10.00'),
            discount_amount=Decimal('0.00'),
        )
        return order, line

    def _create_order_payload(self, number='PO-SCREEN-1'):
        return {
            'supplier': str(self.supplier.pk),
            'branch': str(self.branch.pk),
            'order_number': number,
            'order_date': '2026-10-01',
            'lines-TOTAL_FORMS': '3',
            'lines-INITIAL_FORMS': '0',
            'lines-MIN_NUM_FORMS': '1',
            'lines-MAX_NUM_FORMS': '1000',
            'lines-0-product': str(self.product.pk),
            'lines-0-quantity': '5',
            'lines-0-unit_cost': '10.00',
            'lines-0-discount_amount': '0.00',
            'lines-1-product': '',
            'lines-1-quantity': '',
            'lines-1-unit_cost': '',
            'lines-1-discount_amount': '',
            'lines-2-product': '',
            'lines-2-quantity': '',
            'lines-2-unit_cost': '',
            'lines-2-discount_amount': '',
        }

    def test_draft_order_and_receipt_can_be_edited_before_commitment(self):
        response = self.client.post(reverse('purchase_order_create'), self._create_order_payload('PO-EDIT-1'))
        self.assertEqual(response.status_code, 302)
        order = PurchaseOrder.objects.get(order_number='PO-EDIT-1')
        line = order.lines.get()
        response = self.client.post(
            reverse('purchase_order_edit', args=[order.pk]),
            {
                'supplier': str(self.supplier.pk),
                'branch': str(self.branch.pk),
                'order_number': order.order_number,
                'order_date': '2026-10-01',
                'lines-TOTAL_FORMS': '4',
                'lines-INITIAL_FORMS': '1',
                'lines-MIN_NUM_FORMS': '1',
                'lines-MAX_NUM_FORMS': '1000',
                'lines-0-id': str(line.pk),
                'lines-0-order': str(order.pk),
                'lines-0-product': str(self.product.pk),
                'lines-0-quantity': '7',
                'lines-0-unit_cost': '12.00',
                'lines-0-discount_amount': '0.00',
                **{
                    f'lines-{index}-{field}': value
                    for index in range(1, 4)
                    for field, value in {
                        'id': '',
                        'order': str(order.pk),
                        'product': '',
                        'quantity': '',
                        'unit_cost': '',
                        'discount_amount': '',
                    }.items()
                },
            },
        )
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.quantity, Decimal('7.000'))
        self.assertEqual(line.unit_cost, Decimal('12.00'))

        order.confirm(user=self.user)
        response = self.client.get(reverse('purchase_order_edit', args=[order.pk]))
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            reverse('goods_receipt_create', args=[order.pk]),
            {
                'receipt_number': 'GR-EDIT-1',
                'receipt_date': '2026-10-02',
                'warehouse': str(self.warehouse.pk),
                f'quantity_{line.pk}': '2.000',
            },
        )
        self.assertEqual(response.status_code, 302)
        receipt = GoodsReceipt.objects.get(receipt_number='GR-EDIT-1')
        response = self.client.post(
            reverse('goods_receipt_edit', args=[receipt.pk]),
            {
                'receipt_number': 'GR-EDIT-1-REV',
                'receipt_date': '2026-10-03',
                'warehouse': str(self.warehouse.pk),
                f'quantity_{line.pk}': '3.000',
            },
        )
        self.assertEqual(response.status_code, 302)
        receipt.refresh_from_db()
        self.assertEqual(receipt.receipt_number, 'GR-EDIT-1-REV')
        self.assertEqual(receipt.lines.get().quantity, Decimal('3.000'))

        receipt.post()
        response = self.client.get(reverse('goods_receipt_edit', args=[receipt.pk]))
        self.assertEqual(response.status_code, 404)

    def test_order_partial_receipt_and_supplier_invoice_flow(self):
        response = self.client.post(reverse('purchase_order_create'), self._create_order_payload())
        self.assertEqual(response.status_code, 302)
        order = PurchaseOrder.objects.get(order_number='PO-SCREEN-1')
        self.assertEqual(order.status, 'draft')
        self.assertEqual(order.lines.count(), 1)

        response = self.client.post(reverse('purchase_order_detail', args=[order.pk]), {'action': 'confirm'})
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')

        order_line = order.lines.get()
        response = self.client.post(
            reverse('goods_receipt_create', args=[order.pk]),
            {
                'receipt_number': 'GR-SCREEN-1',
                'receipt_date': '2026-10-02',
                'warehouse': str(self.warehouse.pk),
                f'quantity_{order_line.pk}': '3.000',
            },
        )
        self.assertEqual(response.status_code, 302)
        receipt = GoodsReceipt.objects.get(receipt_number='GR-SCREEN-1')
        self.assertEqual(receipt.status, 'draft')
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('0.000'))

        response = self.client.post(reverse('goods_receipt_detail', args=[receipt.pk]), {'action': 'post'})
        self.assertEqual(response.status_code, 302)
        receipt.refresh_from_db()
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(receipt.status, 'posted')
        self.assertEqual(order.status, 'partially_received')
        self.assertEqual(self.product.stock_quantity, Decimal('3.000'))
        receipt.post()
        self.assertEqual(
            StockMovement.objects.filter(reference_model='GoodsReceipt', reference_id=receipt.pk).count(),
            1,
        )

        receipt_line = receipt.lines.get()
        response = self.client.post(
            reverse('purchase_invoice_from_receipt', args=[receipt.pk]),
            {
                'invoice_number': 'PI-FROM-RECEIPT-1',
                'invoice_date': '2026-10-03',
                'due_date': '2026-10-31',
                'place_of_supply': '',
                f'line_{receipt_line.pk}_quantity': '2.000',
                f'line_{receipt_line.pk}_unit_cost': '11.00',
                f'line_{receipt_line.pk}_discount': '0.00',
            },
        )
        self.assertEqual(response.status_code, 302)
        invoice = PurchaseInvoice.objects.get(invoice_number='PI-FROM-RECEIPT-1')
        invoice_line = invoice.lines.get()
        self.assertEqual(invoice_line.receipt_line_id, receipt_line.pk)
        self.assertEqual(invoice_line.purchase_order_line_id, order_line.pk)
        self.assertEqual(invoice.subtotal, Decimal('22.00'))

        response = self.client.post(reverse('purchase_invoice_detail', args=[invoice.pk]), {'action': 'post'})
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(invoice.status, 'posted')
        self.assertEqual(self.product.stock_quantity, Decimal('3.000'))
        self.assertEqual(JournalEntry.objects.filter(reference='PI-PI-FROM-RECEIPT-1').count(), 1)
        invoice.total_amount = Decimal('0.00')
        with self.assertRaises(ValidationError):
            invoice.save()
        with self.assertRaises(ValidationError):
            PurchaseInvoice.objects.filter(pk=invoice.pk).update(total_amount=Decimal('0.00'))
        with self.assertRaises(ValidationError):
            PurchaseInvoice.objects.filter(pk=invoice.pk).update(status='draft')
        self.assertEqual(
            StockMovement.objects.filter(reference_model='GoodsReceipt', reference_id=receipt.pk).count(),
            1,
        )
        self.assertFalse(StockMovement.objects.filter(reference_model='PurchaseInvoice', reference_id=invoice.pk).exists())

        response = self.client.post(
            reverse('goods_receipt_create', args=[order.pk]),
            {
                'receipt_number': 'GR-SCREEN-2',
                'receipt_date': '2026-10-04',
                'warehouse': str(self.warehouse.pk),
                f'quantity_{order_line.pk}': '2.000',
            },
        )
        self.assertEqual(response.status_code, 302)
        second_receipt = GoodsReceipt.objects.get(receipt_number='GR-SCREEN-2')
        second_receipt.post()
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, 'received')
        self.assertEqual(self.product.stock_quantity, Decimal('5.000'))

    def test_linked_invoice_cannot_exceed_posted_received_quantity(self):
        order, order_line = self._order(quantity=Decimal('2.000'), number='PO-INVOICE-LIMIT')
        order.confirm(user=self.user)
        receipt = GoodsReceipt.objects.create(
            organization=self.organization,
            branch=self.branch,
            warehouse=self.warehouse,
            order=order,
            receipt_number='GR-INVOICE-LIMIT',
            receipt_date='2026-10-02',
            created_by=self.user,
        )
        receipt_line = GoodsReceiptLine.objects.create(
            goods_receipt=receipt,
            order_line=order_line,
            quantity=Decimal('2.000'),
        )
        receipt.post()
        invoice = PurchaseInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=self.supplier,
            invoice_number='PI-OVER-RECEIVED',
            invoice_date='2026-10-03',
            subtotal=Decimal('30.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('30.00'),
        )
        for _ in range(2):
            PurchaseInvoiceLine.objects.create(
                invoice=invoice,
                product=self.product,
                receipt_line=receipt_line,
                purchase_order_line=order_line,
                quantity=Decimal('1.500'),
                unit_cost=Decimal('10.00'),
                line_total=Decimal('15.00'),
            )
        with self.assertRaises(ValidationError):
            invoice.post()
        invoice.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(invoice.status, 'draft')
        self.assertEqual(self.product.stock_quantity, Decimal('2.000'))
        self.assertFalse(JournalEntry.objects.filter(reference='PI-PI-OVER-RECEIVED').exists())

    def test_receipt_aggregates_repeated_product_lines_at_weighted_unit_cost(self):
        order, first_line = self._order(quantity=Decimal('2.000'), number='PO-WEIGHTED-COST')
        second_line = PurchaseOrderLine.objects.create(
            order=order,
            product=self.product,
            quantity=Decimal('3.000'),
            unit_cost=Decimal('12.00'),
            discount_amount=Decimal('0.00'),
        )
        first_line.unit_cost = Decimal('10.00')
        first_line.save()
        order.confirm(user=self.user)
        receipt = GoodsReceipt.objects.create(
            organization=self.organization,
            branch=self.branch,
            warehouse=self.warehouse,
            order=order,
            receipt_number='GR-WEIGHTED-COST',
            receipt_date='2026-10-02',
            created_by=self.user,
        )
        GoodsReceiptLine.objects.create(goods_receipt=receipt, order_line=first_line, quantity=Decimal('2.000'))
        GoodsReceiptLine.objects.create(goods_receipt=receipt, order_line=second_line, quantity=Decimal('3.000'))
        receipt.post()
        movement = StockMovement.objects.get(reference_model='GoodsReceipt', reference_id=receipt.pk)
        self.assertEqual(movement.quantity, Decimal('5.000'))
        self.assertEqual(movement.unit_cost, Decimal('11.20'))

    def test_receipt_form_rejects_over_receipt_and_duplicate_reference(self):
        order, line = self._order()
        order.confirm(user=self.user)
        payload = {
            'receipt_number': 'GR-OVER',
            'receipt_date': '2026-10-02',
            'warehouse': str(self.warehouse.pk),
            f'quantity_{line.pk}': '5.001',
        }
        response = self.client.post(reverse('goods_receipt_create', args=[order.pk]), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(GoodsReceipt.objects.exists())

        payload[f'quantity_{line.pk}'] = '5.000'
        response = self.client.post(reverse('goods_receipt_create', args=[order.pk]), payload)
        self.assertEqual(response.status_code, 302)
        payload['receipt_number'] = 'GR-OVER'
        response = self.client.post(reverse('goods_receipt_create', args=[order.pk]), payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(GoodsReceipt.objects.filter(receipt_number='GR-OVER').count(), 1)

    def test_receipt_post_rolls_back_stock_and_status_on_failure(self):
        order, line = self._order()
        order.confirm(user=self.user)
        receipt = GoodsReceipt.objects.create(
            organization=self.organization,
            branch=self.branch,
            warehouse=self.warehouse,
            order=order,
            receipt_number='GR-ROLLBACK',
            receipt_date='2026-10-02',
            created_by=self.user,
        )
        GoodsReceiptLine.objects.create(goods_receipt=receipt, order_line=line, quantity=Decimal('2.000'))
        with patch('purchases.models.adjust_stock', side_effect=ValidationError('stock posting failed')):
            with self.assertRaises(ValidationError):
                receipt.post()
        receipt.refresh_from_db()
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(receipt.status, 'draft')
        self.assertEqual(order.status, 'confirmed')
        self.assertEqual(self.product.stock_quantity, Decimal('0.000'))
        self.assertFalse(StockMovement.objects.filter(reference_model='GoodsReceipt', reference_id=receipt.pk).exists())

    def test_purchase_screens_enforce_membership_and_object_scope(self):
        order, _ = self._order()
        outsider = User.objects.create_user(
            username='purchase-staff',
            email='purchase-staff@example.test',
            password='test-password',
            organization=self.organization,
            branch=self.branch,
        )
        OrganizationMembership.objects.create(
            user=outsider,
            organization=self.organization,
            role='staff',
            is_active=True,
        )
        self.client.force_login(outsider)
        response = self.client.post(reverse('purchase_order_detail', args=[order.pk]), {'action': 'confirm'})
        self.assertEqual(response.status_code, 403)

        other_org = Organization.objects.create(name='Other Purchasing Co', slug='other-purchasing-co')
        other_branch = Branch.objects.create(organization=other_org, name='Other', code='OTHER')
        other_supplier = Supplier.objects.create(organization=other_org, name='Other supplier')
        foreign_order = PurchaseOrder.objects.create(
            organization=other_org,
            branch=other_branch,
            supplier=other_supplier,
            order_number='PO-OTHER',
            order_date='2026-10-01',
        )
        response = self.client.get(reverse('purchase_order_detail', args=[foreign_order.pk]))
        self.assertEqual(response.status_code, 404)

    def test_receipt_and_purchase_order_status_cannot_be_changed_by_direct_save(self):
        with self.assertRaises(ValidationError):
            PurchaseOrder.objects.create(
                organization=self.organization,
                branch=self.branch,
                supplier=self.supplier,
                order_number='PO-DIRECT',
                order_date='2026-10-01',
                status='confirmed',
            )
        order, line = self._order()
        with self.assertRaises(ValidationError):
            PurchaseOrder.objects.filter(pk=order.pk).update(status='confirmed')
        order.confirm(user=self.user)
        with self.assertRaises(ValidationError):
            PurchaseOrderLine.objects.filter(pk=line.pk).update(quantity=Decimal('6.000'))
        receipt = GoodsReceipt.objects.create(
            organization=self.organization,
            branch=self.branch,
            warehouse=self.warehouse,
            order=order,
            receipt_number='GR-DIRECT',
            receipt_date='2026-10-02',
        )
        receipt.status = 'posted'
        with self.assertRaises(ValidationError):
            receipt.save()
        receipt.refresh_from_db()
        GoodsReceiptLine.objects.create(goods_receipt=receipt, order_line=line, quantity=Decimal('2.000'))
        receipt.post()
        receipt_line = receipt.lines.get()
        with self.assertRaises(ValidationError):
            GoodsReceiptLine.objects.filter(pk=receipt_line.pk).update(quantity=Decimal('1.000'))
        with self.assertRaises(ValidationError):
            GoodsReceipt.objects.filter(pk=receipt.pk).update(status='draft')
        with self.assertRaises(ValidationError):
            receipt_line.delete()

    def _invoice_payload(self, *, counterparty_field, counterparty_id, number, price_field, price):
        payload = {
            counterparty_field: str(counterparty_id),
            'branch': str(self.branch.pk),
            'invoice_number': number,
            'invoice_date': '2026-10-01',
            'due_date': '2026-10-31',
            'place_of_supply': '',
            'lines-TOTAL_FORMS': '3',
            'lines-INITIAL_FORMS': '0',
            'lines-MIN_NUM_FORMS': '1',
            'lines-MAX_NUM_FORMS': '1000',
            'lines-0-product': str(self.product.pk),
            'lines-0-quantity': '2',
            price_field: str(price),
            'lines-0-discount_amount': '0.00',
            'lines-1-product': '',
            'lines-1-quantity': '',
            f'lines-1-{price_field.split("-")[-1]}': '',
            'lines-1-discount_amount': '',
            'lines-2-product': '',
            'lines-2-quantity': '',
            f'lines-2-{price_field.split("-")[-1]}': '',
            'lines-2-discount_amount': '',
        }
        return payload

    def test_purchase_and_sales_draft_posting_integrates_stock_and_accounting(self):
        purchase_payload = self._invoice_payload(
            counterparty_field='supplier',
            counterparty_id=self.supplier.pk,
            number='SCREEN-PI-1',
            price_field='lines-0-unit_cost',
            price='10.00',
        )
        response = self.client.post(reverse('purchase_invoice_create'), purchase_payload)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('purchase_invoice_list'))
        purchase = PurchaseInvoice.objects.get(invoice_number='SCREEN-PI-1')
        self.assertEqual(purchase.status, 'draft')
        response = self.client.post(
            reverse('purchase_invoice_detail', args=[purchase.pk]),
            {'action': 'post'},
        )
        self.assertRedirects(response, reverse('purchase_invoice_detail', args=[purchase.pk]))
        purchase.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(purchase.status, 'posted')
        self.assertEqual(self.product.stock_quantity, Decimal('2.000'))
        self.assertEqual(
            StockMovement.objects.filter(
                reference_model='PurchaseInvoice',
                reference_id=purchase.pk,
            ).count(),
            1,
        )
        purchase.post()
        self.assertEqual(
            JournalEntry.objects.filter(reference=f'PI-{purchase.invoice_number}').count(),
            1,
        )

        sales_payload = self._invoice_payload(
            counterparty_field='customer',
            counterparty_id=self.customer.pk,
            number='SCREEN-SI-1',
            price_field='lines-0-unit_price',
            price='15.00',
        )
        response = self.client.post(reverse('sales_invoice_create'), sales_payload)
        self.assertRedirects(response, reverse('sales_invoice_list'))
        sale = SalesInvoice.objects.get(invoice_number='SCREEN-SI-1')
        response = self.client.post(
            reverse('sales_invoice_detail', args=[sale.pk]),
            {'action': 'post'},
        )
        self.assertRedirects(response, reverse('sales_invoice_detail', args=[sale.pk]))
        sale.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(sale.status, 'posted')
        self.assertEqual(sale.total_amount, Decimal('30.00'))
        self.assertEqual(self.product.stock_quantity, Decimal('0.000'))
        self.assertEqual(
            StockMovement.objects.filter(
                reference_model='SalesInvoice',
                reference_id=sale.pk,
            ).count(),
            1,
        )
        sale.post()
        self.assertEqual(
            JournalEntry.objects.filter(reference__startswith='SI-SCREEN-SI-1').count(),
            2,
        )

    def test_screens_are_organization_scoped_and_creation_requires_role(self):
        other = Organization.objects.create(name='Other Co', slug='other-screen-co')
        other_customer = Customer.objects.create(organization=other, name='Foreign customer')
        foreign_invoice = SalesInvoice.objects.create(
            organization=other,
            customer=other_customer,
            invoice_number='FOREIGN-SI',
            invoice_date='2026-10-01',
        )
        response = self.client.get(reverse('sales_invoice_detail', args=[foreign_invoice.pk]))
        self.assertEqual(response.status_code, 404)
        foreign_order = SalesOrder.objects.create(
            organization=other,
            customer=other_customer,
            order_number='FOREIGN-SO',
            order_date='2026-10-01',
        )
        self.assertEqual(
            self.client.get(reverse('sales_order_detail', args=[foreign_order.pk])).status_code,
            404,
        )
        self.assertNotContains(self.client.get(reverse('sales_invoice_list')), 'FOREIGN-SI')

        no_role = User.objects.create_user(
            username='screen-viewer',
            email='screen-viewer@example.test',
            password='test-password',
            organization=self.organization,
        )
        OrganizationMembership.objects.create(
            user=no_role,
            organization=self.organization,
            role='viewer',
            is_active=True,
        )
        self.client.force_login(no_role)
        self.assertEqual(self.client.get(reverse('sales_invoice_create')).status_code, 403)
        self.assertEqual(self.client.get(reverse('purchase_invoice_create')).status_code, 403)
        self.assertEqual(self.client.get(reverse('sales_order_create')).status_code, 403)

    def test_inventory_screen_shows_real_stock_and_product_creation_is_scoped(self):
        other_branch = Branch.objects.create(
            organization=self.organization,
            name='Other',
            code='OTHER',
        )
        other_warehouse = Warehouse.objects.create(
            organization=self.organization,
            branch=other_branch,
            name='Other warehouse',
            code='WH2',
        )
        adjust_stock(
            self.product,
            other_warehouse,
            Decimal('50.000'),
            'purchase',
            'OtherBranchReceipt',
            9,
            Decimal('10.00'),
        )
        response = self.client.get(reverse('inventory_home'), {'low': '1'})
        self.assertContains(response, 'SCREEN-1')
        self.assertContains(response, 'Low stock')
        self.assertNotContains(response, 'OtherBranchReceipt')
        response = self.client.post(reverse('product_create'), {
            'sku': 'SCREEN-2',
            'name': 'Second screen product',
            'category': 'General',
            'unit': 'pcs',
            'cost_price': '4.00',
            'sales_price': '6.00',
            'reorder_level': '1.00',
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('inventory_home'))
        created = Product.objects.get(sku='SCREEN-2')
        self.assertEqual(created.organization, self.organization)
        self.assertEqual(created.stock_quantity, Decimal('0'))

    def test_stock_adjustment_is_scoped_idempotent_and_rejects_negative_balance(self):
        payload = {
            'product': self.product.pk,
            'warehouse': self.warehouse.pk,
            'reference_number': '1042',
            'quantity_change': '3.000',
            'unit_cost': '12.500000',
            'reason': 'Physical count correction',
        }
        response = self.client.post(reverse('stock_adjustment'), payload)
        self.assertRedirects(response, reverse('inventory_home'))
        response = self.client.post(reverse('stock_adjustment'), payload)
        self.assertRedirects(response, reverse('inventory_home'))
        self.assertEqual(
            StockMovement.objects.filter(
                reference_model='ManualAdjustment',
                reference_id=1042,
            ).count(),
            1,
        )
        self.assertEqual(
            AuditEvent.objects.filter(action='stock_adjustment').count(),
            1,
        )
        self.assertIn(
            'Physical count correction',
            AuditEvent.objects.get(action='stock_adjustment').details,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('3.000'))
        payload['quantity_change'] = '4.000'
        response = self.client.post(reverse('stock_adjustment'), payload)
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('3.000'))

        payload['reference_number'] = '1043'
        payload['quantity_change'] = '-5.000'
        response = self.client.post(reverse('stock_adjustment'), payload)
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('3.000'))

    def test_warehouse_transfer_posts_paired_movements_atomically_and_is_idempotent(self):
        destination = Warehouse.objects.create(
            organization=self.organization,
            branch=self.branch,
            name='Second warehouse',
            code='WH2',
        )
        adjust_stock(
            self.product,
            self.warehouse,
            Decimal('5.000'),
            'purchase',
            'OpeningReceipt',
            31,
            Decimal('10.00'),
        )
        payload = {
            'reference': 'TR-001',
            'transfer_date': '2026-10-10',
            'product': str(self.product.pk),
            'source_warehouse': str(self.warehouse.pk),
            'destination_warehouse': str(destination.pk),
            'quantity': '3.000',
        }

        response = self.client.post(reverse('stock_transfer'), payload)
        self.assertRedirects(response, reverse('inventory_home'))
        transfer = StockTransfer.objects.get(reference='TR-001')
        self.assertEqual(transfer.status, 'posted')
        self.assertEqual(warehouse_stock_balance(self.product, self.warehouse), Decimal('2.000'))
        self.assertEqual(warehouse_stock_balance(self.product, destination), Decimal('3.000'))
        self.assertEqual(
            StockMovement.objects.filter(
                reference_model='StockTransfer',
                reference_id=transfer.pk,
            ).count(),
            2,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('5.000'))
        self.assertEqual(
            AuditEvent.objects.filter(action='stock_transfer_posted').count(),
            1,
        )

        transfer.post()
        transfer_movement = StockMovement.objects.filter(
            reference_model='StockTransfer',
            reference_id=transfer.pk,
        ).first()
        with self.assertRaises(ValidationError):
            transfer_movement.quantity = Decimal('-2.000')
            transfer_movement.save()
        with self.assertRaises(ValidationError):
            StockMovement.objects.filter(
                reference_model='StockTransfer',
                reference_id=transfer.pk,
            ).update(quantity=Decimal('0'))
        with self.assertRaises(ValidationError):
            StockMovement.objects.filter(
                reference_model='StockTransfer',
                reference_id=transfer.pk,
            ).delete()
        response = self.client.post(reverse('stock_transfer'), payload)
        self.assertRedirects(response, reverse('inventory_home'))
        self.assertEqual(
            StockMovement.objects.filter(
                reference_model='StockTransfer',
                reference_id=transfer.pk,
            ).count(),
            2,
        )
        self.assertEqual(
            AuditEvent.objects.filter(action='stock_transfer_posted').count(),
            1,
        )
        with self.assertRaises(ValidationError):
            StockTransfer.objects.filter(pk=transfer.pk).update(reference='changed')
        with self.assertRaises(ValidationError):
            StockTransfer.objects.filter(pk=transfer.pk).delete()
        with self.assertRaises(ValidationError):
            transfer.delete()

        payload['quantity'] = '4.000'
        response = self.client.post(reverse('stock_transfer'), payload)
        self.assertEqual(response.status_code, 200)
        transfer.refresh_from_db()
        self.assertEqual(transfer.quantity, Decimal('3.000'))
        self.assertEqual(warehouse_stock_balance(self.product, self.warehouse), Decimal('2.000'))

    def test_transfer_rejects_insufficient_warehouse_stock_and_foreign_warehouse(self):
        other = Warehouse.objects.create(
            organization=self.organization,
            branch=self.branch,
            name='Other warehouse',
            code='WH3',
        )
        payload = {
            'reference': 'TR-002',
            'transfer_date': '2026-10-10',
            'product': str(self.product.pk),
            'source_warehouse': str(self.warehouse.pk),
            'destination_warehouse': str(other.pk),
            'quantity': '1.000',
        }
        response = self.client.post(reverse('stock_transfer'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StockTransfer.objects.filter(reference='TR-002').exists())
        self.assertEqual(StockMovement.objects.count(), 0)

        foreign_org = Organization.objects.create(name='Foreign', slug='transfer-foreign')
        foreign_branch = Branch.objects.create(
            organization=foreign_org,
            name='Foreign branch',
            code='FOREIGN',
        )
        foreign_warehouse = Warehouse.objects.create(
            organization=foreign_org,
            branch=foreign_branch,
            name='Foreign warehouse',
            code='FWH',
        )
        payload['reference'] = 'TR-003'
        payload['destination_warehouse'] = str(foreign_warehouse.pk)
        response = self.client.post(reverse('stock_transfer'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StockTransfer.objects.filter(reference='TR-003').exists())

    def test_sales_order_partial_delivery_invoice_lifecycle_does_not_deduct_stock_twice(self):
        adjust_stock(
            self.product,
            self.warehouse,
            Decimal('5.000'),
            'purchase',
            'OpeningReceipt',
            84,
            Decimal('10.00'),
        )
        self.assertEqual(self.client.get(reverse('sales_order_list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('sales_order_create')).status_code, 200)
        order_payload = {
            'customer': str(self.customer.pk),
            'branch': str(self.branch.pk),
            'order_number': 'SO-100',
            'order_date': '2026-10-05',
            'lines-TOTAL_FORMS': '3',
            'lines-INITIAL_FORMS': '0',
            'lines-MIN_NUM_FORMS': '1',
            'lines-MAX_NUM_FORMS': '1000',
            'lines-0-product': str(self.product.pk),
            'lines-0-quantity': '5.000',
            'lines-0-unit_price': '15.00',
            'lines-0-discount_amount': '3.00',
            'lines-1-product': '',
            'lines-1-quantity': '',
            'lines-1-unit_price': '',
            'lines-1-discount_amount': '',
            'lines-2-product': '',
            'lines-2-quantity': '',
            'lines-2-unit_price': '',
            'lines-2-discount_amount': '',
        }
        response = self.client.post(reverse('sales_order_create'), order_payload)
        self.assertEqual(response.status_code, 302)
        order = SalesOrder.objects.get(order_number='SO-100')
        self.assertEqual(self.client.get(reverse('sales_order_detail', args=[order.pk])).status_code, 200)
        response = self.client.post(
            reverse('sales_order_detail', args=[order.pk]),
            {'action': 'confirm'},
        )
        self.assertRedirects(response, reverse('sales_order_detail', args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')
        self.assertEqual(AuditEvent.objects.filter(action='sales_order_confirmed').count(), 1)
        order.confirm()

        first_delivery_payload = {
            'delivery_number': 'DN-100-A',
            'delivery_date': '2026-10-06',
            'warehouse': str(self.warehouse.pk),
            f'quantity_{order.lines.get().pk}': '2.000',
        }
        response = self.client.post(
            reverse('sales_delivery_create', args=[order.pk]),
            first_delivery_payload,
        )
        self.assertRedirects(response, reverse('sales_order_detail', args=[order.pk]))
        first_delivery = DeliveryNote.objects.get(delivery_number='DN-100-A')
        self.assertEqual(first_delivery.status, 'dispatched')
        self.assertEqual(
            self.client.get(reverse('sales_invoice_from_delivery', args=[first_delivery.pk])).status_code,
            200,
        )
        with self.assertRaises(ValidationError):
            SalesOrder.objects.filter(pk=order.pk).update(order_number='CHANGED')
        with self.assertRaises(ValidationError):
            SalesOrder.objects.filter(pk=order.pk).delete()
        with self.assertRaises(ValidationError):
            order.order_number = 'CHANGED'
            order.save()
        with self.assertRaises(ValidationError):
            SalesOrderLine.objects.filter(order=order).update(quantity=Decimal('1.000'))
        with self.assertRaises(ValidationError):
            SalesOrderLine.objects.filter(order=order).delete()
        with self.assertRaises(ValidationError):
            DeliveryNote.objects.filter(pk=first_delivery.pk).update(status='draft')
        with self.assertRaises(ValidationError):
            DeliveryNote.objects.filter(pk=first_delivery.pk).delete()
        with self.assertRaises(ValidationError):
            DeliveryNoteLine.objects.filter(delivery_note=first_delivery).update(quantity=Decimal('1.000'))
        with self.assertRaises(ValidationError):
            DeliveryNoteLine.objects.filter(delivery_note=first_delivery).delete()
        self.assertEqual(first_delivery.lines.get().discount_amount, Decimal('1.20'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('3.000'))

        response = self.client.post(
            reverse('sales_delivery_create', args=[order.pk]),
            {
                'delivery_number': 'DN-OVER',
                'delivery_date': '2026-10-07',
                'warehouse': str(self.warehouse.pk),
                f'quantity_{order.lines.get().pk}': '4.000',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DeliveryNote.objects.filter(delivery_number='DN-OVER').exists())

        first_invoice_payload = {
            'invoice_number': 'SI-DN-100-A',
            'invoice_date': '2026-10-06',
            'due_date': '2026-11-05',
            'place_of_supply': '',
        }
        response = self.client.post(
            reverse('sales_invoice_from_delivery', args=[first_delivery.pk]),
            first_invoice_payload,
        )
        self.assertEqual(response.status_code, 302)
        first_invoice = SalesInvoice.objects.get(invoice_number='SI-DN-100-A')
        self.assertEqual(first_invoice.lines.get().quantity, Decimal('2.000'))
        response = self.client.post(
            reverse('sales_invoice_detail', args=[first_invoice.pk]),
            {'action': 'post'},
        )
        self.assertRedirects(response, reverse('sales_invoice_detail', args=[first_invoice.pk]))
        first_invoice.refresh_from_db()
        self.assertEqual(first_invoice.status, 'posted')
        self.assertEqual(
            StockMovement.objects.filter(
                reference_model='SalesInvoice',
                reference_id=first_invoice.pk,
            ).count(),
            0,
        )
        self.assertEqual(
            StockMovement.objects.filter(
                reference_model='DeliveryNote',
                reference_id=first_delivery.pk,
            ).count(),
            1,
        )
        first_delivery.post()

        response = self.client.post(
            reverse('sales_delivery_create', args=[order.pk]),
            {
                'delivery_number': 'DN-100-B',
                'delivery_date': '2026-10-08',
                'warehouse': str(self.warehouse.pk),
                f'quantity_{order.lines.get().pk}': '3.000',
            },
        )
        self.assertRedirects(response, reverse('sales_order_detail', args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(order.status, 'fulfilled')
        second_delivery = DeliveryNote.objects.get(delivery_number='DN-100-B')
        self.assertEqual(second_delivery.lines.get().discount_amount, Decimal('1.80'))
        response = self.client.post(
            reverse('sales_invoice_from_delivery', args=[second_delivery.pk]),
            {
                'invoice_number': 'SI-DN-100-B',
                'invoice_date': '2026-10-08',
                'due_date': '2026-11-07',
                'place_of_supply': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        second_invoice = SalesInvoice.objects.get(invoice_number='SI-DN-100-B')
        second_invoice.post()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('0.000'))
        for entry in JournalEntry.objects.filter(
            reference__in=[
                f'SI-{first_invoice.invoice_number}-revenue',
                f'SI-{first_invoice.invoice_number}-cogs',
                f'SI-{second_invoice.invoice_number}-revenue',
                f'SI-{second_invoice.invoice_number}-cogs',
            ]
        ):
            totals = entry.lines.aggregate(debits=Sum('debit'), credits=Sum('credit'))
            self.assertEqual(totals['debits'], totals['credits'])
