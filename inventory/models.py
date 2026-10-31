from decimal import Decimal, ROUND_HALF_UP
from contextvars import ContextVar

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from accounting.models import ensure_accounting_period_open, round_currency
from organizations.models import Branch, Organization


_valuation_write_enabled = ContextVar('inventory_valuation_write_enabled', default=False)


class Warehouse(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='warehouses')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='warehouses')
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=30)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [('organization', 'code')]
        ordering = ['name']

    def __str__(self):
        return f'{self.organization.name} / {self.name}'


class Product(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='products')
    sku = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=120, blank=True)
    unit = models.CharField(max_length=20, default='pcs')
    cost_price = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    sales_price = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    reorder_level = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    stock_quantity = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal('0'))
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.sku} - {self.name}'


class StockMovementQuerySet(models.QuerySet):
    def bulk_create(self, objs, *args, **kwargs):
        raise ValidationError('Use the inventory costing service to record stock movements.')

    def update(self, **kwargs):
        raise ValidationError('Stock movements are immutable; post a correcting movement instead.')

    def delete(self):
        raise ValidationError('Stock movements are immutable and cannot be deleted.')

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError('Stock movements are immutable; post a correcting movement instead.')


class StockMovement(models.Model):
    MOVEMENT_TYPES = [
        ('purchase', 'Purchase'),
        ('sale', 'Sale'),
        ('adjustment', 'Adjustment'),
        ('transfer', 'Transfer'),
        ('sales_return', 'Sales return'),
        ('purchase_return', 'Purchase return'),
    ]

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='stock_movements')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='stock_movements')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES)
    reference_model = models.CharField(max_length=100, blank=True)
    reference_id = models.PositiveIntegerField(null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=24, decimal_places=6, default=Decimal('0'))
    created_at = models.DateTimeField(auto_now_add=True)

    objects = StockMovementQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['product', 'warehouse', 'movement_type', 'reference_id'],
                condition=models.Q(reference_model='StockTransfer', reference_id__isnull=False),
                name='uniq_stock_transfer_movement_per_warehouse',
            ),
            models.UniqueConstraint(
                fields=['product', 'warehouse', 'movement_type', 'reference_id'],
                condition=models.Q(reference_model__in=['CustomerReturn', 'SupplierReturn'], reference_id__isnull=False),
                name='uniq_stock_return_movement_per_warehouse',
            ),
        ]

    def __str__(self):
        return f'{self.product.name} {self.quantity}'

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Stock movements are immutable; post a correcting movement instead.')
        if not _valuation_write_enabled.get():
            raise ValidationError('Use the inventory costing service to record stock movements.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Stock movements are immutable and cannot be deleted.')


class InventoryValuationBalanceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Use the inventory costing service to change a valuation balance.')

    def delete(self):
        raise ValidationError('Valuation balances cannot be deleted.')

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError('Use the inventory costing service to change a valuation balance.')


class InventoryValuationBalance(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name='inventory_valuation_balances',
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='valuation_balances')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='valuation_balances')
    quantity_on_hand = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal('0'))
    inventory_value = models.DecimalField(max_digits=24, decimal_places=2, default=Decimal('0'))
    is_valued = models.BooleanField(default=True)
    valuation_start_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = InventoryValuationBalanceQuerySet.as_manager()

    class Meta:
        ordering = ['product__sku', 'warehouse__code']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'product', 'warehouse'],
                name='uniq_inventory_valuation_position',
            ),
            models.CheckConstraint(
                condition=models.Q(inventory_value__gte=0),
                name='inventory_value_nonnegative',
            ),
        ]

    @property
    def average_unit_cost(self):
        if not self.is_valued or not self.quantity_on_hand:
            return None
        return (self.inventory_value / self.quantity_on_hand).quantize(
            Decimal('0.000001'), rounding=ROUND_HALF_UP
        )


class InventoryValuationEntryQuerySet(models.QuerySet):
    def bulk_create(self, objs, *args, **kwargs):
        raise ValidationError('Use the inventory costing service to record valuation history.')

    def update(self, **kwargs):
        raise ValidationError('Valuation history is immutable; post a correcting entry instead.')

    def delete(self):
        raise ValidationError('Valuation history cannot be deleted.')

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError('Valuation history is immutable; post a correcting entry instead.')


class InventoryValuationEntry(models.Model):
    EVENT_TYPES = [
        ('movement', 'Stock movement'),
        ('opening', 'Opening valuation'),
        ('revaluation', 'Inventory value adjustment'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='inventory_valuation_entries')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='valuation_entries')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='valuation_entries')
    balance = models.ForeignKey(InventoryValuationBalance, on_delete=models.PROTECT, related_name='entries')
    stock_movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='valuation_entry',
    )
    event_type = models.CharField(max_length=16, choices=EVENT_TYPES)
    source_model = models.CharField(max_length=100)
    source_id = models.PositiveIntegerField(null=True, blank=True)
    effective_at = models.DateTimeField(default=timezone.now)
    quantity_change = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal('0'))
    value_change = models.DecimalField(max_digits=24, decimal_places=2, default=Decimal('0'))
    unit_cost = models.DecimalField(max_digits=30, decimal_places=6, default=Decimal('0'))
    quantity_after = models.DecimalField(max_digits=18, decimal_places=3)
    value_after = models.DecimalField(max_digits=24, decimal_places=2)
    is_valued_after = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        'core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_valuation_entries'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = InventoryValuationEntryQuerySet.as_manager()

    class Meta:
        ordering = ['effective_at', 'pk']
        constraints = [
            models.UniqueConstraint(
                fields=['source_model', 'source_id', 'product', 'warehouse'],
                condition=models.Q(source_id__isnull=False),
                name='uniq_inventory_valuation_source_position',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Valuation history is immutable; post a correcting entry instead.')
        if not _valuation_write_enabled.get():
            raise ValidationError('Use the inventory costing service to record valuation history.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Valuation history cannot be deleted.')


def warehouse_stock_balance(product, warehouse):
    balance = StockMovement.objects.filter(
        product=product,
        warehouse=warehouse,
    ).aggregate(quantity=Sum('quantity'))['quantity']
    return balance or Decimal('0')


def _cost_unit(value, quantity):
    if not quantity:
        return Decimal('0.000000')
    return (Decimal(value) / Decimal(quantity)).quantize(
        Decimal('0.000001'), rounding=ROUND_HALF_UP
    )


def _get_locked_valuation_balance(product, warehouse):
    ledger_quantity = warehouse_stock_balance(product, warehouse)
    balance, _ = InventoryValuationBalance.objects.get_or_create(
        organization_id=product.organization_id,
        product=product,
        warehouse=warehouse,
        defaults={
            'quantity_on_hand': ledger_quantity,
            'inventory_value': Decimal('0.00'),
            'is_valued': ledger_quantity == 0,
            'valuation_start_date': timezone.localdate() if ledger_quantity == 0 else None,
        },
    )
    balance = InventoryValuationBalance.objects.select_for_update().get(pk=balance.pk)
    if balance.quantity_on_hand != ledger_quantity:
        raise ValidationError(
            f'Valuation quantity for {product.sku} in {warehouse.code} does not match the stock movement ledger. '
            'Reconcile the position before posting another movement.'
        )
    return balance


@transaction.atomic
def apply_stock_movement(
    product,
    warehouse,
    quantity,
    movement_type,
    reference_model='',
    reference_id=None,
    unit_cost=None,
    *,
    value_override=None,
    update_product_aggregate=True,
    user=None,
):
    """Append one immutable stock movement and its weighted-average valuation event."""
    quantity = Decimal(quantity)
    if quantity == 0:
        raise ValidationError('Stock movement quantity cannot be zero.')
    locked_product = Product.objects.select_for_update().get(pk=product.pk)
    locked_warehouse = Warehouse.objects.select_for_update().get(pk=warehouse.pk)
    if locked_warehouse.organization_id != locked_product.organization_id:
        raise ValidationError('Warehouse and product must belong to the same organization.')
    if locked_warehouse.branch.organization_id != locked_warehouse.organization_id:
        raise ValidationError('Warehouse branch must belong to its organization.')
    if not locked_warehouse.is_active:
        raise ValidationError('Stock movements require an active warehouse.')

    if reference_model and reference_id is not None:
        existing = StockMovement.objects.filter(
            product=locked_product,
            warehouse=locked_warehouse,
            movement_type=movement_type,
            reference_model=reference_model,
            reference_id=reference_id,
        ).first()
        if existing:
            if existing.quantity != quantity:
                raise ValidationError('This stock movement reference was already used with different values.')
            if not InventoryValuationEntry.objects.filter(stock_movement=existing).exists():
                raise ValidationError(
                    'This legacy stock movement has no valuation event. Resolve its opening valuation before retrying.'
                )
            return existing

    if update_product_aggregate:
        ledger_total = StockMovement.objects.filter(product=locked_product).aggregate(
            quantity=Sum('quantity')
        )['quantity'] or Decimal('0')
        # New inbound stock can be received into its warehouse while a separate
        # legacy aggregate quantity remains unallocated. Keep that legacy delta
        # unchanged; outgoing stock still requires resolving it first.
        if locked_product.stock_quantity != ledger_total and (
            quantity < 0 or locked_product.stock_quantity < ledger_total
        ):
            raise ValidationError(
                f'Organization-wide stock for {locked_product.sku} does not match warehouse movements. '
                'Allocate and value legacy stock before issuing stock.'
            )

    balance = _get_locked_valuation_balance(locked_product, locked_warehouse)
    old_quantity = balance.quantity_on_hand
    new_quantity = old_quantity + quantity
    if new_quantity < 0:
        raise ValidationError('Stock movement would make warehouse stock negative.')

    if quantity < 0:
        if old_quantity > 0 and not balance.is_valued:
            raise ValidationError(
                f'{locked_product.sku} in {locked_warehouse.code} has legacy stock with unknown value. '
                'Record an explicit opening valuation before issuing or transferring it.'
            )
        if movement_type == 'transfer' and not balance.is_valued:
            raise ValidationError('An unvalued stock position cannot be transferred.')
        if new_quantity == 0:
            movement_value = balance.inventory_value
        elif old_quantity > 0:
            movement_value = round_currency(balance.inventory_value * abs(quantity) / old_quantity)
        else:
            movement_value = Decimal('0.00')
        if movement_value > balance.inventory_value:
            movement_value = balance.inventory_value
        value_change = -movement_value
        movement_unit_cost = _cost_unit(movement_value, abs(quantity))
        new_value = balance.inventory_value + value_change
        valued_after = balance.is_valued
    else:
        if unit_cost is None:
            raise ValidationError('Incoming stock requires an explicit unit cost.')
        incoming_unit_cost = Decimal(unit_cost)
        if incoming_unit_cost < 0:
            raise ValidationError('Incoming stock unit cost cannot be negative.')
        if value_override is not None:
            incoming_value = round_currency(Decimal(value_override))
        else:
            incoming_value = round_currency(quantity * incoming_unit_cost)
        if incoming_value < 0:
            raise ValidationError('Incoming stock value cannot be negative.')
        if old_quantity > 0 and not balance.is_valued and movement_type == 'transfer':
            raise ValidationError(
                f'Destination {locked_warehouse.code} contains unvalued legacy stock. '
                'Record an explicit opening valuation before transferring into it.'
            )
        if balance.is_valued or old_quantity == 0:
            value_change = incoming_value
            new_value = balance.inventory_value + value_change
            valued_after = True
        else:
            # Preserve explicit movement cost, but do not claim the aggregate is valued
            # while a legacy portion remains unknown.
            value_change = Decimal('0.00')
            new_value = balance.inventory_value
            valued_after = False
        movement_unit_cost = incoming_unit_cost.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)

    if new_quantity == 0 and valued_after:
        new_value = Decimal('0.00')
    token = _valuation_write_enabled.set(True)
    try:
        movement = StockMovement.objects.create(
            product=locked_product,
            warehouse=locked_warehouse,
            quantity=quantity,
            movement_type=movement_type,
            reference_model=reference_model,
            reference_id=reference_id,
            unit_cost=movement_unit_cost,
        )
        balance.quantity_on_hand = new_quantity
        balance.inventory_value = new_value
        balance.is_valued = valued_after
        if valued_after and balance.valuation_start_date is None:
            balance.valuation_start_date = timezone.localdate()
        balance.save(update_fields=['quantity_on_hand', 'inventory_value', 'is_valued', 'valuation_start_date', 'updated_at'])
        InventoryValuationEntry.objects.create(
            organization=locked_product.organization,
            product=locked_product,
            warehouse=locked_warehouse,
            balance=balance,
            stock_movement=movement,
            event_type='opening' if reference_model == 'OpeningBalance' else 'movement',
            source_model=reference_model or 'StockMovement',
            source_id=reference_id,
            quantity_change=quantity,
            value_change=value_change,
            unit_cost=movement_unit_cost,
            quantity_after=new_quantity,
            value_after=new_value,
            is_valued_after=valued_after,
            created_by=user,
        )
    finally:
        _valuation_write_enabled.reset(token)

    if update_product_aggregate:
        updated_total = locked_product.stock_quantity + quantity
        if updated_total < 0:
            raise ValidationError('Stock movement would make the organization-wide product quantity negative.')
        locked_product.stock_quantity = updated_total
        locked_product.save(update_fields=['stock_quantity'])
        product.stock_quantity = updated_total
    return movement


@transaction.atomic
def adjust_inventory_value(*, product, warehouse, value_change, source_model, source_id, user=None):
    """Post an auditable value-only adjustment without inventing a quantity movement."""
    value_change = round_currency(Decimal(value_change))
    locked_product = Product.objects.select_for_update().get(pk=product.pk)
    locked_warehouse = Warehouse.objects.select_for_update().get(pk=warehouse.pk)
    if locked_warehouse.organization_id != locked_product.organization_id:
        raise ValidationError('Warehouse and product must belong to the same organization.')
    if locked_warehouse.branch.organization_id != locked_warehouse.organization_id:
        raise ValidationError('Warehouse branch must belong to its organization.')
    if not locked_warehouse.is_active:
        raise ValidationError('Inventory value adjustments require an active warehouse.')
    balance = _get_locked_valuation_balance(locked_product, locked_warehouse)
    existing = InventoryValuationEntry.objects.filter(
        source_model=source_model,
        source_id=source_id,
        product=locked_product,
        warehouse=locked_warehouse,
    ).first()
    if existing:
        if existing.value_change != (value_change if balance.is_valued else Decimal('0.00')):
            raise ValidationError('This inventory value adjustment reference was already used with different values.')
        return existing
    if balance.quantity_on_hand == 0 and value_change:
        raise ValidationError('Inventory value cannot be adjusted for a zero-quantity position.')
    if balance.is_valued:
        new_value = balance.inventory_value + value_change
        if new_value < 0:
            raise ValidationError('Inventory value adjustment would make the balance negative.')
        recorded_change = value_change
    else:
        # Preserve the unknown opening balance. The adjustment is visible in event
        # history, while reports continue to show the position as incomplete.
        new_value = balance.inventory_value
        recorded_change = Decimal('0.00')
    balance.inventory_value = new_value
    balance.save(update_fields=['inventory_value', 'updated_at'])
    token = _valuation_write_enabled.set(True)
    try:
        return InventoryValuationEntry.objects.create(
            organization=locked_product.organization,
            product=locked_product,
            warehouse=locked_warehouse,
            balance=balance,
            event_type='revaluation',
            source_model=source_model,
            source_id=source_id,
            quantity_change=Decimal('0'),
            value_change=recorded_change,
            unit_cost=_cost_unit(abs(recorded_change), balance.quantity_on_hand),
            quantity_after=balance.quantity_on_hand,
            value_after=new_value,
            is_valued_after=balance.is_valued,
            created_by=user,
        )
    finally:
        _valuation_write_enabled.reset(token)


@transaction.atomic
def record_opening_valuation(*, product, warehouse, quantity, inventory_value, user):
    """Value a current legacy warehouse position without rewriting its old movements."""
    quantity = Decimal(quantity)
    inventory_value = round_currency(Decimal(inventory_value))
    if quantity < 0 or inventory_value < 0 or (quantity == 0 and inventory_value != 0):
        raise ValidationError('Opening quantity and value cannot be negative.')
    locked_product = Product.objects.select_for_update().get(pk=product.pk)
    locked_warehouse = Warehouse.objects.select_for_update().get(pk=warehouse.pk)
    ensure_accounting_period_open(locked_product.organization_id, timezone.localdate())
    if locked_warehouse.organization_id != locked_product.organization_id:
        raise ValidationError('Warehouse and product must belong to the same organization.')
    if locked_warehouse.branch.organization_id != locked_warehouse.organization_id:
        raise ValidationError('Warehouse branch must belong to its organization.')
    if not locked_warehouse.is_active:
        raise ValidationError('Opening valuations require an active warehouse.')
    ledger_quantity = warehouse_stock_balance(locked_product, locked_warehouse)
    movement_total = StockMovement.objects.filter(product=locked_product).aggregate(
        quantity=Sum('quantity')
    )['quantity'] or Decimal('0')
    unallocated = locked_product.stock_quantity - movement_total
    if quantity != ledger_quantity:
        if unallocated <= 0 or quantity != ledger_quantity + unallocated:
            raise ValidationError(
                f'Opening quantity must match the ledger quantity {ledger_quantity} '
                'plus any unallocated legacy quantity.'
            )
    balance = _get_locked_valuation_balance(locked_product, locked_warehouse)
    if balance.is_valued and quantity == ledger_quantity:
        raise ValidationError('This warehouse position already has a complete valuation.')

    if quantity > ledger_quantity:
        unallocated_quantity = quantity - ledger_quantity
        if balance.is_valued:
            additional_value = inventory_value - balance.inventory_value
            if additional_value < 0:
                raise ValidationError('Opening total value cannot be below the value already recorded for this warehouse.')
            unit_cost = _cost_unit(additional_value, unallocated_quantity)
            movement = apply_stock_movement(
                locked_product,
                locked_warehouse,
                unallocated_quantity,
                'adjustment',
                'OpeningBalance',
                locked_product.pk,
                unit_cost,
                value_override=additional_value,
                update_product_aggregate=False,
                user=user,
            )
            from core.models import AuditEvent

            AuditEvent.objects.create(
                user=user,
                organization=locked_product.organization,
                action='inventory_opening_valuation',
                details=(
                    f'Allocated {unallocated_quantity} unallocated legacy units for {locked_product.sku} '
                    f'to {locked_warehouse.code}; resulting value {inventory_value}.'
                ),
            )
            return movement.valuation_entry
        apply_stock_movement(
            locked_product,
            locked_warehouse,
            unallocated_quantity,
            'adjustment',
            'OpeningBalance',
            locked_product.pk,
            Decimal('0'),
            value_override=Decimal('0'),
            update_product_aggregate=False,
            user=user,
        )
        balance.refresh_from_db()

    balance.inventory_value = inventory_value
    balance.is_valued = True
    balance.valuation_start_date = timezone.localdate()
    balance.save(update_fields=['inventory_value', 'is_valued', 'valuation_start_date', 'updated_at'])
    token = _valuation_write_enabled.set(True)
    try:
        entry = InventoryValuationEntry.objects.create(
            organization=locked_product.organization,
            product=locked_product,
            warehouse=locked_warehouse,
            balance=balance,
            event_type='opening',
            source_model='OpeningValuation',
            source_id=balance.pk,
            quantity_change=Decimal('0'),
            value_change=inventory_value,
            unit_cost=_cost_unit(inventory_value, quantity),
            quantity_after=quantity,
            value_after=inventory_value,
            is_valued_after=True,
            created_by=user,
        )
    finally:
        _valuation_write_enabled.reset(token)
    from core.models import AuditEvent

    AuditEvent.objects.create(
        user=user,
        organization=locked_product.organization,
        action='inventory_opening_valuation',
        details=(
            f'Opening valuation recorded for {locked_product.sku} in {locked_warehouse.code}: '
            f'{quantity} units at total value {inventory_value}. No historical movement was changed.'
        ),
    )
    return entry


def allocate_legacy_stock_to_only_warehouse(product, warehouse):
    locked_product = Product.objects.get(pk=product.pk)
    active_warehouses = Warehouse.objects.filter(
        organization=locked_product.organization,
        is_active=True,
    )
    if active_warehouses.count() != 1 or active_warehouses.first().pk != warehouse.pk:
        raise ValidationError(
            'Legacy organization-wide stock has no warehouse allocation. '
            'Record an approved valued opening balance before dispatch.'
        )
    movement_total = StockMovement.objects.filter(
        product=locked_product,
    ).aggregate(quantity=Sum('quantity'))['quantity'] or Decimal('0')
    unallocated = locked_product.stock_quantity - movement_total
    if unallocated < 0:
        raise ValidationError(
            f'Product balance for {locked_product.sku} is lower than its stock ledger.'
        )
    if unallocated == 0:
        return None
    raise ValidationError(
        f'{unallocated} legacy units of {locked_product.sku} are not valued or allocated. '
        'Record an approved opening valuation before dispatch.'
    )


class StockTransferQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.filter(status='posted').exists() or kwargs.get('status') == 'posted':
            raise ValidationError('Posted stock transfers cannot be changed in bulk.')
        return super().update(**kwargs)

    def delete(self):
        if self.filter(status='posted').exists():
            raise ValidationError('Posted stock transfers cannot be deleted.')
        return super().delete()


class StockTransfer(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('posted', 'Posted')]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name='stock_transfers',
    )
    reference = models.CharField(max_length=50)
    transfer_date = models.DateField(default=timezone.localdate)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='stock_transfers')
    source_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name='outgoing_transfers',
    )
    destination_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name='incoming_transfers',
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey(
        'core.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_transfers',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    objects = StockTransferQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'reference'],
                name='uniq_stock_transfer_reference_per_org',
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name='stock_transfer_quantity_positive',
            ),
            models.CheckConstraint(
                condition=~models.Q(source_warehouse=models.F('destination_warehouse')),
                name='stock_transfer_warehouses_differ',
            ),
        ]

    def clean(self):
        errors = {}
        if self.quantity is not None and self.quantity <= 0:
            errors['quantity'] = 'Transfer quantity must be greater than zero.'
        if self.source_warehouse_id and self.destination_warehouse_id:
            if self.source_warehouse_id == self.destination_warehouse_id:
                errors['destination_warehouse'] = 'Choose a different destination warehouse.'
            if self.source_warehouse.organization_id != self.organization_id:
                errors['source_warehouse'] = 'Source warehouse must belong to the organization.'
            if self.destination_warehouse.organization_id != self.organization_id:
                errors['destination_warehouse'] = 'Destination warehouse must belong to the organization.'
            if self.source_warehouse.branch.organization_id != self.organization_id:
                errors['source_warehouse'] = 'Source warehouse branch must belong to the organization.'
            if self.destination_warehouse.branch.organization_id != self.organization_id:
                errors['destination_warehouse'] = 'Destination warehouse branch must belong to the organization.'
        if self.product_id and self.product.organization_id != self.organization_id:
            errors['product'] = 'Product must belong to the organization.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if previous.status == 'posted':
                raise ValidationError('Posted stock transfers cannot be changed.')
            if self.status == 'posted':
                raise ValidationError('Use the post operation to finalize a stock transfer.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == 'posted':
            raise ValidationError('Posted stock transfers cannot be deleted.')
        return super().delete(*args, **kwargs)

    def post(self):
        with transaction.atomic():
            transfer = type(self).objects.select_for_update().get(pk=self.pk)
            if transfer.status == 'posted':
                self.status = transfer.status
                self.posted_at = transfer.posted_at
                return self

            ensure_accounting_period_open(transfer.organization_id, transfer.transfer_date)
            product = Product.objects.select_for_update().get(
                pk=transfer.product_id,
                organization_id=transfer.organization_id,
            )
            warehouses = list(
                Warehouse.objects.select_for_update()
                .filter(pk__in=[transfer.source_warehouse_id, transfer.destination_warehouse_id])
                .order_by('pk')
            )
            if len(warehouses) != 2 or any(
                warehouse.organization_id != transfer.organization_id
                or warehouse.branch.organization_id != transfer.organization_id
                for warehouse in warehouses
            ):
                raise ValidationError('Both transfer warehouses must belong to the organization.')
            source_balance = warehouse_stock_balance(product, transfer.source_warehouse)
            if source_balance < transfer.quantity:
                raise ValidationError(
                    f'Insufficient stock in {transfer.source_warehouse.name}; '
                    f'{source_balance} available, {transfer.quantity} requested.'
                )
            if StockMovement.objects.filter(
                reference_model='StockTransfer',
                reference_id=transfer.pk,
            ).exists():
                raise ValidationError('Stock movements already exist for this draft transfer.')

            outgoing = apply_stock_movement(
                product,
                transfer.source_warehouse,
                -transfer.quantity,
                'transfer',
                'StockTransfer',
                transfer.pk,
                user=transfer.created_by,
            )
            apply_stock_movement(
                product,
                transfer.destination_warehouse,
                transfer.quantity,
                'transfer',
                'StockTransfer',
                transfer.pk,
                outgoing.unit_cost,
                value_override=abs(outgoing.valuation_entry.value_change),
                user=transfer.created_by,
            )
            from core.models import AuditEvent

            AuditEvent.objects.create(
                user=transfer.created_by,
                organization=transfer.organization,
                action='stock_transfer_posted',
                details=(
                    f'Transfer {transfer.reference}: {product.sku}, {transfer.quantity} '
                    f'from {transfer.source_warehouse.code} to {transfer.destination_warehouse.code}.'
                ),
            )
            transfer.status = 'posted'
            from django.utils import timezone

            transfer.posted_at = timezone.now()
            models.Model.save(
                transfer,
                update_fields=['status', 'posted_at'],
            )
            self.status = transfer.status
            self.posted_at = transfer.posted_at
            return self


def adjust_stock(
    product,
    warehouse,
    quantity,
    movement_type,
    reference_model='',
    reference_id=None,
    unit_cost=None,
    *,
    value_override=None,
    update_product_aggregate=True,
    user=None,
):
    return apply_stock_movement(
        product,
        warehouse,
        quantity,
        movement_type,
        reference_model,
        reference_id,
        unit_cost,
        value_override=value_override,
        update_product_aggregate=update_product_aggregate,
        user=user,
    )
