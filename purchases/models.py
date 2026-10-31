from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum

from accounting.models import (
    JournalEntry,
    JournalEntryLine,
    ensure_accounting_period_open,
    ensure_default_accounts,
    round_currency,
)
from inventory.models import (
    Product,
    StockMovement,
    Warehouse,
    adjust_inventory_value,
    adjust_stock,
    warehouse_stock_balance,
)
from organizations.models import Branch, Organization


class Supplier(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='suppliers')
    name = models.CharField(max_length=200)
    gstin = models.CharField(max_length=20, blank=True)
    state_code = models.CharField(max_length=2, blank=True)
    is_reverse_charge_applicable = models.BooleanField(default=False)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    outstanding_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    def clean(self):
        from taxation.models import validate_gstin, validate_state_code

        errors = {}
        self.gstin = self.gstin.strip().upper()
        try:
            validate_gstin(self.gstin)
            validate_state_code(self.state_code)
        except ValidationError as exc:
            errors['gstin' if self.gstin else 'state_code'] = exc.messages
        if self.gstin and self.state_code and self.gstin[:2] != self.state_code:
            errors['gstin'] = 'GSTIN state prefix must match the supplier state code.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PurchaseOrderQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if 'status' in kwargs:
            raise ValidationError('Purchase-order status must change through confirm or cancel.')
        with transaction.atomic():
            orders = list(self.select_for_update().order_by('pk'))
            if any(order.status != 'draft' for order in orders):
                raise ValidationError('Confirmed or completed purchase orders cannot be changed in bulk.')
            return super().update(**kwargs)

    def delete(self):
        with transaction.atomic():
            orders = list(self.select_for_update().order_by('pk'))
            if any(order.status != 'draft' or order.goods_receipts.exists() for order in orders):
                raise ValidationError('Committed or receipt-linked purchase orders cannot be deleted.')
            return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if 'status' in fields:
            raise ValidationError('Purchase-order status must change through confirm or cancel.')
        with transaction.atomic():
            locked = list(
                self.model.objects.select_for_update()
                .filter(pk__in=[obj.pk for obj in objects])
                .order_by('pk')
            )
            if any(order.status != 'draft' for order in locked):
                raise ValidationError('Confirmed or completed purchase orders cannot be changed in bulk.')
            return super().bulk_update(objects, fields, batch_size=batch_size)


class PurchaseOrder(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
        ('partially_received', 'Partially received'),
        ('received', 'Received'),
        ('cancelled', 'Cancelled'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='purchase_orders')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='purchase_orders')
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='purchase_orders')
    order_number = models.CharField(max_length=50, unique=True)
    order_date = models.DateField()
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey(
        'core.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='purchase_orders',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PurchaseOrderQuerySet.as_manager()

    class Meta:
        ordering = ['-order_date', '-id']

    def clean(self):
        errors = {}
        if self.supplier_id and self.organization_id and self.supplier.organization_id != self.organization_id:
            errors['supplier'] = 'Supplier must belong to the purchase order organization.'
        if self.branch_id and self.organization_id and self.branch.organization_id != self.organization_id:
            errors['branch'] = 'Branch must belong to the purchase order organization.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_for_update().get(pk=self.pk)
                if previous.status != 'draft':
                    raise ValidationError('Confirmed or completed purchase orders cannot be edited.')
                if self.status != previous.status:
                    raise ValidationError('Use the confirm or cancel action to change purchase-order status.')
            elif self.status != 'draft':
                raise ValidationError('Purchase orders must be created as drafts and confirmed through the workflow.')
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            order = type(self).objects.select_for_update().get(pk=self.pk)
            if order.status != 'draft' or order.goods_receipts.exists():
                raise ValidationError('Purchase orders with committed status or receipt history cannot be deleted.')
            return super().delete(*args, **kwargs)

    @property
    def total_before_tax(self):
        return sum(
            (
                max(Decimal('0'), line.quantity * line.unit_cost - line.discount_amount)
                for line in self.lines.all()
            ),
            Decimal('0'),
        )

    def confirm(self, user=None):
        from core.models import AuditEvent

        with transaction.atomic():
            order = type(self).objects.select_for_update().get(pk=self.pk)
            if order.status != 'draft' or not order.lines.exists():
                raise ValidationError('Only a non-empty draft purchase order can be confirmed.')
            if (
                order.supplier.organization_id != order.organization_id
                or order.branch.organization_id != order.organization_id
                or order.lines.exclude(product__organization_id=order.organization_id).exists()
            ):
                raise ValidationError('Purchase-order supplier, branch and products must belong to its organization.')
            for line in order.lines.all():
                if line.quantity <= 0 or line.unit_cost < 0 or line.discount_amount < 0:
                    raise ValidationError('Purchase order contains an invalid quantity, cost or discount.')
                if not line.product.is_active:
                    raise ValidationError(f'{line.product.name} is inactive and cannot be ordered.')
                if line.discount_amount > line.quantity * line.unit_cost:
                    raise ValidationError('A purchase-order discount cannot exceed its line amount.')
            ensure_accounting_period_open(order.organization_id, order.order_date)
            order.status = 'confirmed'
            models.Model.save(order, update_fields=['status'])
            AuditEvent.objects.create(
                user=user,
                organization=order.organization,
                action='purchase_order_confirmed',
                details=f'Purchase order {order.order_number} confirmed.',
            )
            self.status = order.status
            return self

    def cancel(self, user=None):
        from core.models import AuditEvent

        with transaction.atomic():
            order = type(self).objects.select_for_update().get(pk=self.pk)
            if order.status == 'cancelled':
                self.status = order.status
                return self
            if order.status not in {'draft', 'confirmed'}:
                raise ValidationError('Only draft or unreceived purchase orders can be cancelled.')
            if order.goods_receipts.filter(status='posted').exists():
                raise ValidationError('A purchase order with posted receipts cannot be cancelled.')
            order.status = 'cancelled'
            models.Model.save(order, update_fields=['status'])
            AuditEvent.objects.create(
                user=user,
                organization=order.organization,
                action='purchase_order_cancelled',
                details=f'Purchase order {order.order_number} cancelled.',
            )
            self.status = order.status
            return self


class PurchaseOrderLineQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if 'order' in kwargs or 'order_id' in kwargs:
            raise ValidationError('Purchase-order lines cannot be moved to another order in bulk.')
        with transaction.atomic():
            order_ids = self.values_list('order_id', flat=True).distinct()
            orders = list(PurchaseOrder.objects.select_for_update().filter(pk__in=order_ids).order_by('pk'))
            if any(order.status != 'draft' for order in orders):
                raise ValidationError('Purchase-order lines cannot be changed after confirmation.')
            return super().update(**kwargs)

    def delete(self):
        with transaction.atomic():
            order_ids = self.values_list('order_id', flat=True).distinct()
            orders = list(PurchaseOrder.objects.select_for_update().filter(pk__in=order_ids).order_by('pk'))
            if any(order.status != 'draft' for order in orders):
                raise ValidationError('Purchase-order lines cannot be deleted after confirmation.')
            return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if 'order' in fields or 'order_id' in fields:
            raise ValidationError('Purchase-order lines cannot be moved to another order in bulk.')
        with transaction.atomic():
            order_ids = self.filter(pk__in=[obj.pk for obj in objects]).values_list('order_id', flat=True).distinct()
            orders = list(PurchaseOrder.objects.select_for_update().filter(pk__in=order_ids).order_by('pk'))
            if any(order.status != 'draft' for order in orders):
                raise ValidationError('Purchase-order lines cannot be changed after confirmation.')
            return super().bulk_update(objects, fields, batch_size=batch_size)


class PurchaseOrderLine(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='purchase_order_lines')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name='purchase_order_quantity_positive'),
            models.CheckConstraint(condition=models.Q(unit_cost__gte=0), name='purchase_order_cost_nonnegative'),
            models.CheckConstraint(condition=models.Q(discount_amount__gte=0), name='po_discount_nonnegative'),
            models.CheckConstraint(
                condition=models.Q(discount_amount__lte=models.F('quantity') * models.F('unit_cost')),
                name='po_discount_within_amount',
            ),
        ]

    objects = PurchaseOrderLineQuerySet.as_manager()

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.get(pk=self.pk)
                if previous.order_id != self.order_id:
                    raise ValidationError('Purchase-order lines cannot be moved to another order.')
                order_id = previous.order_id
            else:
                order_id = self.order_id
            order = PurchaseOrder.objects.select_for_update().get(pk=order_id)
            if order.status != 'draft':
                raise ValidationError('Purchase-order lines cannot be changed after confirmation.')
            self.full_clean()
            return super().save(*args, **kwargs)

    def clean(self):
        errors = {}
        if self.order_id and self.product_id and self.order.organization_id != self.product.organization_id:
            errors['product'] = 'Product must belong to the purchase order organization.'
        if self.discount_amount is not None:
            if self.discount_amount < 0:
                errors['discount_amount'] = 'Discount cannot be negative.'
            elif self.quantity is not None and self.unit_cost is not None:
                if self.discount_amount > self.quantity * self.unit_cost:
                    errors['discount_amount'] = 'Discount cannot exceed the line amount.'
        if errors:
            raise ValidationError(errors)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            order = PurchaseOrder.objects.select_for_update().get(pk=self.order_id)
            if order.status != 'draft':
                raise ValidationError('Purchase-order lines cannot be deleted after confirmation.')
            return super().delete(*args, **kwargs)


class GoodsReceiptQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if 'status' in kwargs:
            raise ValidationError('Goods receipt status and posted records cannot be changed in bulk.')
        with transaction.atomic():
            receipts = list(self.select_for_update().order_by('pk'))
            if any(receipt.status == 'posted' for receipt in receipts):
                raise ValidationError('Goods receipt status and posted records cannot be changed in bulk.')
            return super().update(**kwargs)

    def delete(self):
        with transaction.atomic():
            receipts = list(self.select_for_update().order_by('pk'))
            if any(receipt.status == 'posted' for receipt in receipts):
                raise ValidationError('Posted goods receipts cannot be deleted.')
            return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if 'status' in fields:
            raise ValidationError('Posted goods receipts cannot be changed in bulk.')
        with transaction.atomic():
            receipts = list(
                self.model.objects.select_for_update()
                .filter(pk__in=[obj.pk for obj in objects])
                .order_by('pk')
            )
            if any(receipt.status == 'posted' for receipt in receipts):
                raise ValidationError('Posted goods receipts cannot be changed in bulk.')
            return super().bulk_update(objects, fields, batch_size=batch_size)


class GoodsReceipt(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('posted', 'Posted')]

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='goods_receipts')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='goods_receipts')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='goods_receipts')
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name='goods_receipts')
    receipt_number = models.CharField(max_length=50, unique=True)
    receipt_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey(
        'core.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='goods_receipts',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = GoodsReceiptQuerySet.as_manager()

    class Meta:
        ordering = ['-receipt_date', '-id']

    def clean(self):
        errors = {}
        if self.order_id and self.organization_id:
            if self.order.organization_id != self.organization_id:
                errors['order'] = 'Purchase order must belong to the receipt organization.'
            if self.branch_id and self.order.branch_id != self.branch_id:
                errors['branch'] = 'Goods receipt branch must match the purchase order branch.'
        if self.branch_id and self.organization_id and self.branch.organization_id != self.organization_id:
            errors['branch'] = 'Branch must belong to the goods receipt organization.'
        if self.warehouse_id and self.branch_id:
            if self.warehouse.organization_id != self.organization_id or self.warehouse.branch_id != self.branch_id:
                errors['warehouse'] = 'Warehouse must belong to the goods receipt branch and organization.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_for_update().get(pk=self.pk)
                if previous.status == 'posted':
                    raise ValidationError('Posted goods receipts cannot be changed.')
                if self.status != previous.status:
                    raise ValidationError('Use the post action to change goods receipt status.')
            elif self.status != 'draft':
                raise ValidationError('Goods receipts must be created as drafts and posted through the workflow.')
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            receipt = type(self).objects.select_for_update().get(pk=self.pk)
            if receipt.status == 'posted':
                raise ValidationError('Posted goods receipts cannot be deleted.')
            return super().delete(*args, **kwargs)

    def post(self):
        from core.models import AuditEvent

        with transaction.atomic():
            receipt = type(self).objects.select_for_update().get(pk=self.pk)
            if receipt.status == 'posted':
                self.status = receipt.status
                return self
            order = PurchaseOrder.objects.select_for_update().get(pk=receipt.order_id)
            if order.status not in {'confirmed', 'partially_received'}:
                raise ValidationError('Goods receipt requires a confirmed purchase order.')
            if (
                receipt.organization_id != order.organization_id
                or receipt.branch_id != order.branch_id
                or receipt.warehouse.organization_id != order.organization_id
                or receipt.warehouse.branch_id != order.branch_id
                or not receipt.warehouse.is_active
                or order.supplier.organization_id != order.organization_id
            ):
                raise ValidationError('Receipt, warehouse, branch, supplier and order must share the organization.')
            if receipt.receipt_date < order.order_date:
                raise ValidationError('Receipt date cannot be earlier than the purchase order date.')
            ensure_accounting_period_open(receipt.organization_id, receipt.receipt_date)
            lines = list(receipt.lines.select_related('order_line__product').order_by('order_line_id'))
            if not lines:
                raise ValidationError('Goods receipt has no lines.')
            by_product = {}
            for line in lines:
                order_line = PurchaseOrderLine.objects.select_for_update().get(pk=line.order_line_id)
                if order_line.order_id != order.pk or order_line.product.organization_id != order.organization_id:
                    raise ValidationError('Receipt lines must refer to products on this purchase order.')
                received = GoodsReceiptLine.objects.filter(
                    order_line=order_line,
                    goods_receipt__status='posted',
                ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
                if line.quantity <= 0 or received + line.quantity > order_line.quantity:
                    raise ValidationError(f'Receipt quantity exceeds the outstanding order quantity for {order_line.product.name}.')
                item = by_product.setdefault(
                    order_line.product_id,
                    {'product': order_line.product, 'quantity': Decimal('0'), 'cost_value': Decimal('0')},
                )
                item['quantity'] += line.quantity
                effective_unit_cost = (
                    order_line.quantity * order_line.unit_cost - order_line.discount_amount
                ) / order_line.quantity
                item['cost_value'] += line.quantity * effective_unit_cost

            if StockMovement.objects.filter(reference_model='GoodsReceipt', reference_id=receipt.pk).exists():
                raise ValidationError('Stock movements already exist for this draft receipt.')
            for item in by_product.values():
                average_cost = item['cost_value'] / item['quantity']
                adjust_stock(
                    item['product'],
                    receipt.warehouse,
                    item['quantity'],
                    'purchase',
                    'GoodsReceipt',
                    receipt.pk,
                    average_cost,
                    user=receipt.created_by,
                )

            for movement in StockMovement.objects.filter(
                reference_model='GoodsReceipt',
                reference_id=receipt.pk,
            ).select_related('valuation_entry'):
                valuation = movement.valuation_entry
                if not valuation.is_valued_after:
                    raise ValidationError(
                        f'Cannot post receipt {receipt.receipt_number} while {movement.product.sku} '
                        f'in {movement.warehouse.code} has an incomplete valuation. Record its opening value first.'
                    )

            receipt.status = 'posted'
            models.Model.save(receipt, update_fields=['status'])
            remaining = False
            for order_line in order.lines.all():
                received = GoodsReceiptLine.objects.filter(
                    order_line=order_line,
                    goods_receipt__status='posted',
                ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
                if received < order_line.quantity:
                    remaining = True
                    break
            order.status = 'partially_received' if remaining else 'received'
            models.Model.save(order, update_fields=['status'])
            AuditEvent.objects.create(
                user=receipt.created_by,
                organization=receipt.organization,
                action='purchase_goods_receipt_posted',
                details=f'Goods receipt {receipt.receipt_number} posted for order {order.order_number}.',
            )
            self.status = receipt.status
            return self


class GoodsReceiptLineQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if (
            'goods_receipt' in kwargs
            or 'goods_receipt_id' in kwargs
            or 'order_line' in kwargs
            or 'order_line_id' in kwargs
        ):
            raise ValidationError('Posted goods receipt lines cannot be changed in bulk.')
        with transaction.atomic():
            receipt_ids = self.values_list('goods_receipt_id', flat=True).distinct()
            receipts = list(GoodsReceipt.objects.select_for_update().filter(pk__in=receipt_ids).order_by('pk'))
            if any(receipt.status == 'posted' for receipt in receipts):
                raise ValidationError('Posted goods receipt lines cannot be changed in bulk.')
            return super().update(**kwargs)

    def delete(self):
        with transaction.atomic():
            receipt_ids = self.values_list('goods_receipt_id', flat=True).distinct()
            receipts = list(GoodsReceipt.objects.select_for_update().filter(pk__in=receipt_ids).order_by('pk'))
            if any(receipt.status == 'posted' for receipt in receipts):
                raise ValidationError('Posted goods receipt lines cannot be deleted in bulk.')
            return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if 'goods_receipt' in fields or 'goods_receipt_id' in fields or 'order_line' in fields or 'order_line_id' in fields:
            raise ValidationError('Posted goods receipt lines cannot be changed in bulk.')
        with transaction.atomic():
            receipt_ids = self.filter(pk__in=[obj.pk for obj in objects]).values_list(
                'goods_receipt_id', flat=True
            ).distinct()
            receipts = list(GoodsReceipt.objects.select_for_update().filter(pk__in=receipt_ids).order_by('pk'))
            if any(receipt.status == 'posted' for receipt in receipts):
                raise ValidationError('Posted goods receipt lines cannot be changed in bulk.')
            return super().bulk_update(objects, fields, batch_size=batch_size)


class GoodsReceiptLine(models.Model):
    goods_receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name='lines')
    order_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name='receipt_lines')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['goods_receipt', 'order_line'], name='uniq_receipt_purchase_order_line'),
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name='goods_receipt_quantity_positive'),
        ]

    objects = GoodsReceiptLineQuerySet.as_manager()

    def clean(self):
        if self.goods_receipt_id and self.order_line_id:
            if self.goods_receipt.order_id != self.order_line.order_id:
                raise ValidationError({'order_line': 'Receipt line must belong to the receipt purchase order.'})

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.get(pk=self.pk)
                if previous.goods_receipt_id != self.goods_receipt_id:
                    raise ValidationError('Goods receipt lines cannot be moved to another receipt.')
                receipt_id = previous.goods_receipt_id
            else:
                receipt_id = self.goods_receipt_id
            receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt_id)
            if receipt.status == 'posted':
                raise ValidationError('Posted goods receipt lines cannot be changed.')
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            receipt = GoodsReceipt.objects.select_for_update().get(pk=self.goods_receipt_id)
            if receipt.status == 'posted':
                raise ValidationError('Posted goods receipt lines cannot be deleted.')
            return super().delete(*args, **kwargs)


class PurchaseInvoiceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if 'status' in kwargs:
            raise ValidationError('Purchase invoice status must change through its posting workflow.')
        with transaction.atomic():
            invoices = list(self.select_for_update().order_by('pk'))
            if any(invoice.status == 'posted' for invoice in invoices):
                raise ValidationError('Posted purchase invoices cannot be changed in bulk.')
            return super().update(**kwargs)

    def delete(self):
        with transaction.atomic():
            invoices = list(self.select_for_update().order_by('pk'))
            if any(invoice.status == 'posted' for invoice in invoices):
                raise ValidationError('Posted purchase invoices cannot be deleted.')
            return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if 'status' in fields:
            raise ValidationError('Purchase invoice status must change through its posting workflow.')
        with transaction.atomic():
            invoices = list(
                self.model.objects.select_for_update()
                .filter(pk__in=[obj.pk for obj in objects])
                .order_by('pk')
            )
            if any(invoice.status == 'posted' for invoice in invoices):
                raise ValidationError('Posted purchase invoices cannot be changed in bulk.')
            return super().bulk_update(objects, fields, batch_size=batch_size)


class PurchaseInvoice(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('posted', 'Posted'), ('cancelled', 'Cancelled')]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='purchase_invoices')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_invoices')
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='purchase_invoices')
    invoice_number = models.CharField(max_length=50, unique=True)
    invoice_date = models.DateField()
    place_of_supply = models.CharField(max_length=2, blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-invoice_date', '-id']

    objects = PurchaseInvoiceQuerySet.as_manager()

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_for_update().get(pk=self.pk)
                if previous.status == 'posted':
                    raise ValidationError('Posted purchase invoices cannot be changed.')
                if self.status != previous.status:
                    raise ValidationError('Use the posting workflow to change purchase invoice status.')
            elif self.status != 'draft':
                raise ValidationError('Purchase invoices must be created as drafts and posted through the workflow.')
            return super().save(*args, **kwargs)

    def __str__(self):
        return self.invoice_number

    def validate_totals(self):
        lines = list(self.lines.all())
        if any(line.discount_amount < 0 or line.discount_amount > line.line_total for line in lines):
            raise ValidationError('Line discounts must be between zero and each line amount.')
        line_total = sum((line.line_total - line.discount_amount for line in lines), Decimal('0'))
        if self.subtotal != line_total:
            raise ValidationError('Invoice subtotal does not match the sum of line totals.')
        if self.subtotal + self.tax_amount != self.total_amount:
            raise ValidationError('Invoice total does not match subtotal plus tax.')

    def post(self):
        if self.status == 'posted':
            return self
        with transaction.atomic():
            invoice = PurchaseInvoice.objects.select_for_update().get(pk=self.pk)
            if invoice.status == 'posted':
                self.status = invoice.status
                return self
            if (
                invoice.supplier.organization_id != invoice.organization_id
                or (invoice.branch_id and invoice.branch.organization_id != invoice.organization_id)
            ):
                raise ValidationError('Purchase invoice supplier and branch must belong to its organization.')
            if not invoice.lines.exists():
                raise ValidationError('Purchase invoice has no line items.')

            from taxation.services import (
                calculate_document_tax_lines,
                document_tax_totals,
                persist_tax_transaction,
            )

            tax_lines = calculate_document_tax_lines(invoice, 'purchase')
            if tax_lines:
                totals = document_tax_totals(tax_lines)
                invoice.subtotal = totals['subtotal']
                invoice.tax_amount = totals['tax_amount']
                invoice.total_amount = totals['total_amount']
            else:
                invoice.validate_totals()

            capitalized_tax_by_line = {}
            if tax_lines:
                for tax_line, classification, calculation in tax_lines:
                    if not classification.is_input_tax_credit_eligible:
                        capitalized_tax_by_line[tax_line.pk] = (
                            capitalized_tax_by_line.get(tax_line.pk, Decimal('0'))
                            + calculation['total_tax']
                        )

            default_accounts = ensure_default_accounts(invoice.organization, branch=invoice.branch)
            inventory_account = default_accounts['inventory']
            accounts_payable = default_accounts['accounts_payable']
            gst_input = default_accounts['gst_input']
            gst_payable = default_accounts['gst_payable']
            warehouse = invoice.branch.warehouses.first() if invoice.branch else invoice.organization.warehouses.first()
            if warehouse is None:
                raise ValidationError('No warehouse configured for stock movement.')

            stock_by_product = {}
            receipt_value_deltas = {}
            invoice_lines = list(invoice.lines.select_related('product', 'receipt_line__goods_receipt'))
            requested_by_receipt_line = {}
            for line in invoice_lines:
                if line.receipt_line_id and line.quantity > 0:
                    requested_by_receipt_line[line.receipt_line_id] = (
                        requested_by_receipt_line.get(line.receipt_line_id, Decimal('0')) + line.quantity
                    )
            for line in invoice_lines:
                if line.quantity <= 0:
                    raise ValidationError(f'Quantity for {line.product.name} must be positive.')
                if line.line_total <= 0:
                    raise ValidationError(f'Line total for {line.product.name} must be greater than zero.')
                if line.product.organization_id != invoice.organization_id:
                    raise ValidationError('Purchase invoice products must belong to the invoice organization.')
                if line.receipt_line_id:
                    receipt_line = GoodsReceiptLine.objects.select_for_update().get(pk=line.receipt_line_id)
                    if (
                        receipt_line.goods_receipt.status != 'posted'
                        or receipt_line.goods_receipt.organization_id != invoice.organization_id
                        or receipt_line.goods_receipt.branch_id != invoice.branch_id
                        or receipt_line.order_line.product_id != line.product_id
                        or receipt_line.order_line.order.supplier_id != invoice.supplier_id
                        or (
                            line.purchase_order_line_id
                            and line.purchase_order_line_id != receipt_line.order_line_id
                        )
                    ):
                        raise ValidationError('Invoice line must match a posted receipt for this supplier, branch and product.')
                    invoiced = PurchaseInvoiceLine.objects.filter(
                        receipt_line=receipt_line,
                        invoice__status='posted',
                    ).aggregate(total=models.Sum('quantity'))['total'] or Decimal('0')
                    if invoiced + requested_by_receipt_line[receipt_line.pk] > receipt_line.quantity:
                        raise ValidationError(f'Invoice quantity exceeds the uninvoiced received quantity for {line.product.name}.')
                    order_line = receipt_line.order_line
                    expected_unit_cost = (
                        order_line.quantity * order_line.unit_cost - order_line.discount_amount
                    ) / order_line.quantity
                    expected_cost = round_currency(expected_unit_cost * line.quantity)
                    actual_cost = round_currency(line.line_total - line.discount_amount)
                    actual_cost += capitalized_tax_by_line.get(line.pk, Decimal('0'))
                    key = (line.product_id, receipt_line.goods_receipt.warehouse_id)
                    item = receipt_value_deltas.setdefault(
                        key,
                        {
                            'product': line.product,
                            'warehouse': receipt_line.goods_receipt.warehouse,
                            'quantity': Decimal('0'),
                            'delta': Decimal('0'),
                        },
                    )
                    item['quantity'] += line.quantity
                    item['delta'] += actual_cost - expected_cost
                else:
                    if line.purchase_order_line_id:
                        raise ValidationError('Purchase-order-linked invoice lines must be linked to a posted receipt line.')
                    item = stock_by_product.setdefault(
                        line.product_id,
                        {'product': line.product, 'quantity': Decimal('0'), 'cost_value': Decimal('0')},
                    )
                    item['quantity'] += line.quantity
                    item['cost_value'] += (
                        round_currency(line.line_total - line.discount_amount)
                        + capitalized_tax_by_line.get(line.pk, Decimal('0'))
                    )
            for item in stock_by_product.values():
                product = item['product']
                average_cost = item['cost_value'] / item['quantity']
                adjust_stock(
                    product,
                    warehouse,
                    item['quantity'],
                    'purchase',
                    'PurchaseInvoice',
                    invoice.id,
                    average_cost,
                )

            sold_cost_variance = {}
            for (product_id, warehouse_id), item in receipt_value_deltas.items():
                current_quantity = max(Decimal('0'), warehouse_stock_balance(item['product'], item['warehouse']))
                retained_quantity = min(current_quantity, item['quantity'])
                retained_delta = round_currency(item['delta'] * retained_quantity / item['quantity'])
                sold_delta = item['delta'] - retained_delta
                if retained_delta:
                    adjust_inventory_value(
                        product=item['product'],
                        warehouse=item['warehouse'],
                        value_change=retained_delta,
                        source_model='PurchaseInvoiceRevaluation',
                        source_id=invoice.pk,
                    )
                if sold_delta:
                    sold_cost_variance[product_id] = (
                        sold_cost_variance.get(product_id, Decimal('0')) + sold_delta
                    )

            entry = JournalEntry.objects.create(
                organization=invoice.organization,
                branch=invoice.branch,
                reference=f'PI-{invoice.invoice_number}',
                entry_date=invoice.invoice_date,
                narration=f'Purchase invoice {invoice.invoice_number}',
                created_by=None,
            )
            input_tax = Decimal('0')
            noncreditable_tax = Decimal('0')
            reverse_charge_tax = Decimal('0')
            reverse_charge_input_tax = Decimal('0')
            reverse_charge_noncreditable_tax = Decimal('0')
            if tax_lines:
                for _, classification, calculation in tax_lines:
                    if calculation['is_reverse_charge']:
                        reverse_charge_tax += calculation['total_tax']
                        if classification.is_input_tax_credit_eligible:
                            reverse_charge_input_tax += calculation['total_tax']
                        else:
                            reverse_charge_noncreditable_tax += calculation['total_tax']
                    elif classification.is_input_tax_credit_eligible:
                        input_tax += calculation['total_tax']
                    else:
                        noncreditable_tax += calculation['total_tax']
            else:
                input_tax = invoice.tax_amount
            JournalEntryLine.objects.create(
                entry=entry,
                account=inventory_account,
                description='Inventory purchase and non-creditable tax',
                debit=invoice.subtotal + noncreditable_tax + reverse_charge_noncreditable_tax,
            )
            cogs_account = default_accounts['cost_of_goods_sold']
            for product_id, variance in sorted(sold_cost_variance.items()):
                variance = round_currency(variance)
                if not variance:
                    continue
                description = f'Purchase cost variance for sold stock, product {product_id}'
                if variance > 0:
                    JournalEntryLine.objects.create(
                        entry=entry,
                        account=cogs_account,
                        description=description,
                        debit=variance,
                    )
                    JournalEntryLine.objects.create(
                        entry=entry,
                        account=inventory_account,
                        description='Reduce inventory for cost already sold',
                        credit=variance,
                    )
                else:
                    JournalEntryLine.objects.create(
                        entry=entry,
                        account=inventory_account,
                        description='Increase inventory for lower purchase cost already sold',
                        debit=-variance,
                    )
                    JournalEntryLine.objects.create(
                        entry=entry,
                        account=cogs_account,
                        description=description,
                        credit=-variance,
                    )
            if input_tax + reverse_charge_input_tax:
                JournalEntryLine.objects.create(
                    entry=entry,
                    account=gst_input,
                    description='Recoverable GST input',
                    debit=input_tax + reverse_charge_input_tax,
                )
            if reverse_charge_tax:
                JournalEntryLine.objects.create(
                    entry=entry,
                    account=gst_payable,
                    description='GST payable under reverse charge',
                    credit=reverse_charge_tax,
                )
            JournalEntryLine.objects.create(entry=entry, account=accounts_payable, description='Supplier payable', credit=invoice.total_amount)
            entry.post()
            if tax_lines:
                for line, classification, calculation in tax_lines:
                    persist_tax_transaction(
                        organization=invoice.organization,
                        branch=invoice.branch,
                        transaction_kind='purchase',
                        transaction_date=invoice.invoice_date,
                        source_model='PurchaseInvoice',
                        source_id=invoice.pk,
                        source_line_id=line.pk,
                        tax_code=classification.tax_code,
                        calculation=calculation,
                        is_input_tax_credit_eligible=classification.is_input_tax_credit_eligible,
                    )
            invoice.status = 'posted'
            models.Model.save(invoice, update_fields=['subtotal', 'tax_amount', 'total_amount', 'status'])
            from banking.services import recalculate_supplier_outstanding

            recalculate_supplier_outstanding(invoice.supplier_id)
            self.refresh_from_db()
        return self


class PurchaseInvoiceLineQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if 'invoice' in kwargs or 'invoice_id' in kwargs:
            raise ValidationError('Posted purchase invoice lines cannot be changed in bulk.')
        with transaction.atomic():
            invoice_ids = self.values_list('invoice_id', flat=True).distinct()
            invoices = list(PurchaseInvoice.objects.select_for_update().filter(pk__in=invoice_ids).order_by('pk'))
            if any(invoice.status == 'posted' for invoice in invoices):
                raise ValidationError('Posted purchase invoice lines cannot be changed in bulk.')
            return super().update(**kwargs)

    def delete(self):
        with transaction.atomic():
            invoice_ids = self.values_list('invoice_id', flat=True).distinct()
            invoices = list(PurchaseInvoice.objects.select_for_update().filter(pk__in=invoice_ids).order_by('pk'))
            if any(invoice.status == 'posted' for invoice in invoices):
                raise ValidationError('Posted purchase invoice lines cannot be deleted in bulk.')
            return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if 'invoice' in fields or 'invoice_id' in fields:
            raise ValidationError('Posted purchase invoice lines cannot be changed in bulk.')
        with transaction.atomic():
            invoice_ids = self.filter(pk__in=[obj.pk for obj in objects]).values_list(
                'invoice_id', flat=True
            ).distinct()
            invoices = list(PurchaseInvoice.objects.select_for_update().filter(pk__in=invoice_ids).order_by('pk'))
            if any(invoice.status == 'posted' for invoice in invoices):
                raise ValidationError('Posted purchase invoice lines cannot be changed in bulk.')
            return super().bulk_update(objects, fields, batch_size=batch_size)


class PurchaseInvoiceLine(models.Model):
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='purchase_lines')
    receipt_line = models.ForeignKey(
        GoodsReceiptLine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='invoice_lines',
    )
    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='invoice_lines',
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    line_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    objects = PurchaseInvoiceLineQuerySet.as_manager()

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.get(pk=self.pk)
                if previous.invoice_id != self.invoice_id:
                    raise ValidationError('Purchase invoice lines cannot be moved to another invoice.')
                invoice_id = previous.invoice_id
            else:
                invoice_id = self.invoice_id
            invoice = PurchaseInvoice.objects.select_for_update().get(pk=invoice_id)
            if invoice.status == 'posted':
                raise ValidationError('Posted purchase invoice lines cannot be changed.')
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            invoice = PurchaseInvoice.objects.select_for_update().get(pk=self.invoice_id)
            if invoice.status == 'posted':
                raise ValidationError('Posted purchase invoice lines cannot be deleted.')
            return super().delete(*args, **kwargs)

    def __str__(self):
        return f'{self.product.name} x {self.quantity}'
