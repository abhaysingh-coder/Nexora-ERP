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
    InventoryValuationEntry,
    Product,
    StockMovement,
    Warehouse,
    adjust_stock,
    allocate_legacy_stock_to_only_warehouse,
    warehouse_stock_balance,
)
from organizations.models import Branch, Organization


class Customer(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='customers')
    name = models.CharField(max_length=200)
    gstin = models.CharField(max_length=20, blank=True)
    state_code = models.CharField(max_length=2, blank=True)
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
            errors['gstin'] = 'GSTIN state prefix must match the customer state code.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class SalesInvoice(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('posted', 'Posted'), ('cancelled', 'Cancelled')]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='sales_invoices')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_invoices')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='sales_invoices')
    delivery_note = models.OneToOneField(
        'DeliveryNote',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='invoice',
    )
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
            invoice = SalesInvoice.objects.select_for_update().get(pk=self.pk)
            if invoice.status == 'posted':
                self.status = invoice.status
                return self
            if not invoice.lines.exists():
                raise ValidationError('Sales invoice has no line items.')

            from taxation.services import (
                calculate_document_tax_lines,
                document_tax_totals,
                persist_tax_transaction,
            )

            tax_lines = calculate_document_tax_lines(invoice, 'sale')
            if tax_lines:
                totals = document_tax_totals(tax_lines)
                invoice.subtotal = totals['subtotal']
                invoice.tax_amount = totals['tax_amount']
                invoice.total_amount = totals['total_amount']
            else:
                invoice.validate_totals()

            cogs_by_product = {}
            if invoice.delivery_note_id:
                delivery = invoice.delivery_note
                if delivery.status != 'dispatched':
                    raise ValidationError('A sales invoice can only use a dispatched delivery note.')
                if (
                    delivery.organization_id != invoice.organization_id
                    or delivery.branch_id != invoice.branch_id
                    or delivery.order.customer_id != invoice.customer_id
                ):
                    raise ValidationError('Invoice and delivery note must share organization, branch and customer.')
                delivered = {}
                for line in delivery.lines.select_related('order_line__product'):
                    product_id = line.order_line.product_id
                    delivered[product_id] = delivered.get(product_id, Decimal('0')) + line.quantity
                invoiced = {}
                for line in invoice.lines.all():
                    invoiced[line.product_id] = invoiced.get(line.product_id, Decimal('0')) + line.quantity
                if invoiced != delivered:
                    raise ValidationError('Invoice lines must match the linked delivery note quantities.')
                for movement in StockMovement.objects.filter(
                    reference_model='DeliveryNote',
                    reference_id=delivery.pk,
                    movement_type='sale',
                ).select_related('product'):
                    try:
                        valuation = movement.valuation_entry
                        amount = abs(valuation.value_change)
                    except InventoryValuationEntry.DoesNotExist:
                        # A pre-valuation delivery has a source cost snapshot, but its
                        # aggregate warehouse history remains flagged as incomplete.
                        amount = round_currency(abs(movement.quantity) * movement.unit_cost)
                    cogs_by_product[movement.product_id] = (
                        cogs_by_product.get(movement.product_id, Decimal('0')) + amount
                    )
                if set(cogs_by_product) != set(delivered):
                    raise ValidationError('The dispatched delivery is missing one or more stock cost snapshots.')
            else:
                warehouses = Warehouse.objects.filter(
                    organization=invoice.organization,
                    is_active=True,
                )
                if invoice.branch_id:
                    warehouses = warehouses.filter(branch_id=invoice.branch_id)
                warehouse = warehouses.order_by('pk').first()
                if warehouse is None:
                    raise ValidationError('No warehouse configured for stock movement.')

                stock_by_product = {}
                for line in invoice.lines.select_related('product').all():
                    if line.quantity <= 0:
                        raise ValidationError(f'Quantity for {line.product.name} must be positive.')
                    item = stock_by_product.setdefault(
                        line.product_id,
                        {'product': line.product, 'quantity': Decimal('0'), 'cost_value': Decimal('0')},
                    )
                    item['quantity'] += line.quantity
                    item['cost_value'] += Decimal(line.unit_cost) * line.quantity
                for item in stock_by_product.values():
                    product = item['product']
                    product.refresh_from_db(fields=['stock_quantity'])
                    if product.stock_quantity < item['quantity']:
                        raise ValidationError(f'Insufficient stock for {product.name}.')
                    if warehouse_stock_balance(product, warehouse) < item['quantity']:
                        allocate_legacy_stock_to_only_warehouse(product, warehouse)
                    average_cost = item['cost_value'] / item['quantity']
                    movement = adjust_stock(
                        product,
                        warehouse,
                        -item['quantity'],
                        'sale',
                        'SalesInvoice',
                        invoice.id,
                        average_cost,
                    )
                    cogs_by_product[product.pk] = abs(movement.valuation_entry.value_change)

            default_accounts = ensure_default_accounts(invoice.organization, branch=invoice.branch)
            receivables = default_accounts['accounts_receivable']
            sales_revenue = default_accounts['sales_revenue']
            gst_output = default_accounts['gst_payable']
            cogs = default_accounts['cost_of_goods_sold']
            inventory = default_accounts['inventory']

            revenue_entry = JournalEntry.objects.create(
                organization=invoice.organization,
                branch=invoice.branch,
                reference=f'SI-{invoice.invoice_number}-revenue',
                entry_date=invoice.invoice_date,
                narration=f'Sales invoice {invoice.invoice_number}',
                created_by=None,
            )
            JournalEntryLine.objects.create(entry=revenue_entry, account=receivables, description='Customer receivable', debit=invoice.total_amount)
            JournalEntryLine.objects.create(entry=revenue_entry, account=sales_revenue, description='Sales revenue', credit=invoice.subtotal)
            if invoice.tax_amount:
                JournalEntryLine.objects.create(entry=revenue_entry, account=gst_output, description='GST collected', credit=invoice.tax_amount)
            revenue_entry.post()

            total_cogs = sum(cogs_by_product.values(), Decimal('0.00'))
            if total_cogs:
                cogs_entry = JournalEntry.objects.create(
                    organization=invoice.organization,
                    branch=invoice.branch,
                    reference=f'SI-{invoice.invoice_number}-cogs',
                    entry_date=invoice.invoice_date,
                    narration=f'Weighted-average COGS for {invoice.invoice_number}',
                    created_by=None,
                )
                products = {
                    product.pk: product
                    for product in Product.objects.filter(pk__in=cogs_by_product)
                }
                for product_id, cost_amount in sorted(cogs_by_product.items()):
                    cost_amount = round_currency(cost_amount)
                    if not cost_amount:
                        continue
                    product = products[product_id]
                    JournalEntryLine.objects.create(entry=cogs_entry, account=cogs, description=f'COGS - {product.name}', debit=cost_amount)
                    JournalEntryLine.objects.create(entry=cogs_entry, account=inventory, description=f'Inventory reduction - {product.name}', credit=cost_amount)
                cogs_entry.post()

            if tax_lines:
                for line, classification, calculation in tax_lines:
                    persist_tax_transaction(
                        organization=invoice.organization,
                        branch=invoice.branch,
                        transaction_kind='sale',
                        transaction_date=invoice.invoice_date,
                        source_model='SalesInvoice',
                        source_id=invoice.pk,
                        source_line_id=line.pk,
                        tax_code=classification.tax_code,
                        calculation=calculation,
                        is_input_tax_credit_eligible=False,
                    )
            invoice.status = 'posted'
            invoice.save(update_fields=['subtotal', 'tax_amount', 'total_amount', 'status'])
            from banking.services import recalculate_customer_outstanding

            recalculate_customer_outstanding(invoice.customer_id)
            self.refresh_from_db()
        return self


class SalesInvoiceLine(models.Model):
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='sales_lines')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    unit_price = models.DecimalField(max_digits=18, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    line_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    def __str__(self):
        return f'{self.product.name} x {self.quantity}'


class SalesOrderQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.exclude(status='draft').exists() or kwargs.get('status') not in {None, 'draft'}:
            raise ValidationError('Confirmed or completed sales orders cannot be changed in bulk.')
        return super().update(**kwargs)

    def delete(self):
        if self.exclude(status='draft').exists():
            raise ValidationError('Confirmed or completed sales orders cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self.exclude(status='draft').filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('Confirmed or completed sales orders cannot be changed in bulk.')
        return super().bulk_update(objects, fields, batch_size=batch_size)


class SalesOrder(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
        ('fulfilled', 'Fulfilled'),
        ('cancelled', 'Cancelled'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='sales_orders')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_orders')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='sales_orders')
    order_number = models.CharField(max_length=50, unique=True)
    order_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-order_date', '-id']

    objects = SalesOrderQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if previous.status != 'draft':
                raise ValidationError('Confirmed or completed sales orders cannot be changed.')
            if self.status != previous.status:
                raise ValidationError('Use the confirm or cancel operation to change order status.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != 'draft' or self.delivery_notes.exists():
            raise ValidationError('Sales orders with a committed status or delivery history cannot be deleted.')
        return super().delete(*args, **kwargs)

    def confirm(self, user=None):
        from core.models import AuditEvent

        with transaction.atomic():
            order = type(self).objects.select_for_update().get(pk=self.pk)
            if order.status == 'confirmed':
                self.status = order.status
                return self
            if order.status != 'draft' or not order.lines.exists():
                raise ValidationError('Only a non-empty draft sales order can be confirmed.')
            if order.customer.organization_id != order.organization_id:
                raise ValidationError('Sales order customer must belong to the organization.')
            if order.branch_id and order.branch.organization_id != order.organization_id:
                raise ValidationError('Sales order branch must belong to the organization.')
            if order.lines.exclude(product__organization_id=order.organization_id).exists():
                raise ValidationError('Sales order products must belong to the organization.')
            for line in order.lines.all():
                if (
                    line.quantity <= 0
                    or line.unit_price < 0
                    or line.discount_amount < 0
                    or line.discount_amount > line.quantity * line.unit_price
                ):
                    raise ValidationError('Sales order contains an invalid quantity, price or discount.')
            order.status = 'confirmed'
            models.Model.save(order, update_fields=['status'])
            AuditEvent.objects.create(
                user=user,
                organization=order.organization,
                action='sales_order_confirmed',
                details=f'Sales order {order.order_number} confirmed.',
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
                raise ValidationError('Only draft or unfulfilled confirmed orders can be cancelled.')
            if order.delivery_notes.filter(status='dispatched').exists():
                raise ValidationError('An order with dispatched goods cannot be cancelled.')
            order.status = 'cancelled'
            models.Model.save(order, update_fields=['status'])
            AuditEvent.objects.create(
                user=user,
                organization=order.organization,
                action='sales_order_cancelled',
                details=f'Sales order {order.order_number} cancelled.',
            )
            self.status = order.status
            return self


class SalesOrderLineQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.exclude(order__status='draft').exists():
            raise ValidationError('Sales order lines cannot be changed after confirmation.')
        return super().update(**kwargs)

    def delete(self):
        if self.exclude(order__status='draft').exists():
            raise ValidationError('Sales order lines cannot be deleted after confirmation.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self.exclude(order__status='draft').filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('Sales order lines cannot be changed after confirmation.')
        return super().bulk_update(objects, fields, batch_size=batch_size)


class SalesOrderLine(models.Model):
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='sales_order_lines')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    unit_price = models.DecimalField(max_digits=18, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name='sales_order_line_quantity_positive'),
            models.CheckConstraint(condition=models.Q(unit_price__gte=0), name='sales_order_line_price_nonnegative'),
        ]

    objects = SalesOrderLineQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.order_id and self.order.status != 'draft':
            raise ValidationError('Sales order lines cannot be changed after confirmation.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.order.status != 'draft':
            raise ValidationError('Sales order lines cannot be deleted after confirmation.')
        return super().delete(*args, **kwargs)


class DeliveryNoteQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.filter(status='dispatched').exists() or kwargs.get('status') == 'dispatched':
            raise ValidationError('Dispatched delivery notes cannot be changed in bulk.')
        return super().update(**kwargs)

    def delete(self):
        if self.filter(status='dispatched').exists():
            raise ValidationError('Dispatched delivery notes cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self.filter(pk__in=[obj.pk for obj in objects], status='dispatched').exists():
            raise ValidationError('Dispatched delivery notes cannot be changed in bulk.')
        return super().bulk_update(objects, fields, batch_size=batch_size)


class DeliveryNote(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('dispatched', 'Dispatched')]

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='delivery_notes')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='delivery_notes')
    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.PROTECT,
        related_name='sales_deliveries',
    )
    order = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name='delivery_notes')
    delivery_number = models.CharField(max_length=50, unique=True)
    delivery_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-delivery_date', '-id']

    objects = DeliveryNoteQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk, status='dispatched').exists():
            raise ValidationError('Dispatched delivery notes cannot be changed.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == 'dispatched':
            raise ValidationError('Dispatched delivery notes cannot be deleted.')
        return super().delete(*args, **kwargs)

    def post(self):
        from core.models import AuditEvent
        from inventory.models import Product, Warehouse, adjust_stock

        with transaction.atomic():
            delivery = type(self).objects.select_for_update().get(pk=self.pk)
            if delivery.status == 'dispatched':
                self.status = delivery.status
                return self
            order = SalesOrder.objects.select_for_update().get(pk=delivery.order_id)
            if order.status not in {'confirmed', 'fulfilled'}:
                raise ValidationError('Delivery requires a confirmed sales order.')
            ensure_accounting_period_open(delivery.organization_id, delivery.delivery_date)
            if (
                delivery.organization_id != order.organization_id
                or delivery.branch_id != order.branch_id
                or order.customer.organization_id != order.organization_id
            ):
                raise ValidationError('Delivery must match the order organization and branch.')
            warehouse = Warehouse.objects.select_for_update().filter(
                pk=delivery.warehouse_id,
                organization=delivery.organization,
                branch=delivery.branch,
                is_active=True,
            ).first()
            if warehouse is None:
                raise ValidationError('Select an active warehouse in the delivery branch.')

            lines = list(delivery.lines.select_related('order_line__product').order_by('order_line__product_id'))
            if not lines:
                raise ValidationError('Delivery note has no line items.')
            products = {
                product.pk: product
                for product in Product.objects.select_for_update().filter(
                    pk__in={line.order_line.product_id for line in lines},
                ).order_by('pk')
            }
            quantity_by_product = {}
            for line in lines:
                if line.quantity <= 0:
                    raise ValidationError('Delivery quantities must be greater than zero.')
                order_line = SalesOrderLine.objects.select_for_update().get(pk=line.order_line_id)
                if order_line.order_id != order.pk or order_line.product_id not in products:
                    raise ValidationError('Delivery lines must belong to their sales order.')
                if products[order_line.product_id].organization_id != delivery.organization_id:
                    raise ValidationError('Delivery products must belong to the organization.')
                delivered_before = DeliveryNoteLine.objects.filter(
                    order_line=order_line,
                    delivery_note__status='dispatched',
                ).aggregate(quantity=Sum('quantity'))['quantity'] or Decimal('0')
                if delivered_before + line.quantity > order_line.quantity:
                    raise ValidationError(f'Delivery exceeds the outstanding quantity for {products[order_line.product_id].name}.')
                previously_discounted = DeliveryNoteLine.objects.filter(
                    order_line=order_line,
                    delivery_note__status='dispatched',
                ).aggregate(amount=Sum('discount_amount'))['amount'] or Decimal('0')
                if delivered_before + line.quantity == order_line.quantity:
                    line.discount_amount = order_line.discount_amount - previously_discounted
                else:
                    line.discount_amount = round_currency(
                        order_line.discount_amount * line.quantity / order_line.quantity
                    )
                line.save(update_fields=['discount_amount'])
                quantity_by_product[order_line.product_id] = (
                    quantity_by_product.get(order_line.product_id, Decimal('0')) + line.quantity
                )

            for product_id, quantity in quantity_by_product.items():
                product = products[product_id]
                if warehouse_stock_balance(product, warehouse) < quantity:
                    allocate_legacy_stock_to_only_warehouse(product, warehouse)
                adjust_stock(
                    product,
                    warehouse,
                    -quantity,
                    'sale',
                    'DeliveryNote',
                    delivery.pk,
                    product.cost_price,
                    user=delivery.created_by,
                )

            delivery.status = 'dispatched'
            models.Model.save(delivery, update_fields=['status'])
            fully_delivered = True
            for order_line in order.lines.all():
                delivered_quantity = DeliveryNoteLine.objects.filter(
                    order_line=order_line,
                    delivery_note__status='dispatched',
                ).aggregate(quantity=Sum('quantity'))['quantity'] or Decimal('0')
                if delivered_quantity < order_line.quantity:
                    fully_delivered = False
                    break
            if fully_delivered:
                order.status = 'fulfilled'
                models.Model.save(order, update_fields=['status'])
            AuditEvent.objects.create(
                user=delivery.created_by,
                organization=delivery.organization,
                action='sales_delivery_dispatched',
                details=f'Delivery {delivery.delivery_number} dispatched for order {order.order_number}.',
            )
            self.status = delivery.status
        return self


class DeliveryNoteLineQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.filter(delivery_note__status='dispatched').exists():
            raise ValidationError('Dispatched delivery lines cannot be changed in bulk.')
        return super().update(**kwargs)

    def delete(self):
        if self.filter(delivery_note__status='dispatched').exists():
            raise ValidationError('Dispatched delivery lines cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self.filter(
            pk__in=[obj.pk for obj in objects],
            delivery_note__status='dispatched',
        ).exists():
            raise ValidationError('Dispatched delivery lines cannot be changed in bulk.')
        return super().bulk_update(objects, fields, batch_size=batch_size)


class DeliveryNoteLine(models.Model):
    delivery_note = models.ForeignKey(DeliveryNote, on_delete=models.CASCADE, related_name='lines')
    order_line = models.ForeignKey(SalesOrderLine, on_delete=models.PROTECT, related_name='delivery_lines')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['delivery_note', 'order_line'],
                name='uniq_delivery_order_line',
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name='delivery_quantity_positive',
            ),
        ]

    objects = DeliveryNoteLineQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.delivery_note_id and self.delivery_note.status == 'dispatched':
            raise ValidationError('Dispatched delivery lines cannot be changed.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.delivery_note.status == 'dispatched':
            raise ValidationError('Dispatched delivery lines cannot be deleted.')
        return super().delete(*args, **kwargs)


class CustomerReceipt(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='customer_receipts')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='customer_receipts')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='receipts')
    receipt_number = models.CharField(max_length=50, unique=True)
    receipt_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    payment_method = models.CharField(max_length=50, default='cash')
    status = models.CharField(max_length=20, choices=[('draft', 'Draft'), ('posted', 'Posted')], default='draft')

    def post(self):
        if self.status == 'posted':
            return self
        with transaction.atomic():
            receipt = CustomerReceipt.objects.select_for_update().select_related(
                'organization', 'branch', 'customer'
            ).get(pk=self.pk)
            if receipt.status == 'posted':
                self.status = receipt.status
                return self
            default_accounts = ensure_default_accounts(receipt.organization, branch=receipt.branch)
            cash_account = default_accounts['cash']
            receivable_account = default_accounts['accounts_receivable']
            entry = JournalEntry.objects.create(
                organization=receipt.organization,
                branch=receipt.branch,
                reference=f'CR-{receipt.receipt_number}',
                entry_date=receipt.receipt_date,
                narration=f'Customer receipt {receipt.receipt_number}',
                created_by=None,
            )
            JournalEntryLine.objects.create(entry=entry, account=cash_account, description='Cash received', debit=receipt.amount)
            JournalEntryLine.objects.create(entry=entry, account=receivable_account, description='Receivable cleared', credit=receipt.amount)
            entry.post()
            receipt.status = 'posted'
            receipt.save(update_fields=['status'])
            from banking.services import recalculate_customer_outstanding

            recalculate_customer_outstanding(receipt.customer_id)
            self.status = receipt.status
        return self
